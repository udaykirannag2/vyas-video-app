"""Production guardrails for the Vyas-Video pipeline.

Provides:
  - GuardrailsConfig: per-run budget and threshold settings
  - RunContext: tracks spend, calls, retries, breaker state across all steps
  - @guarded decorator: wraps any pipeline step with retry, timeout, budget,
    breaker, and loop-detection checks
  - CircuitBreaker: per-model/provider failure tracking
  - RunLog: structured observability

Usage in a pipeline step:

    ctx = RunContext()  # or passed from caller
    result = ctx.call("ideation.detect_segments", detect_segments, clean_text,
                       estimated_cost=0.08, model="opus-4.6")

Or as a decorator:

    @guarded(step="screenwriter", model="sonnet-4.6", estimated_cost=0.03)
    def write_script(idea, timed):
        ...
"""
# Allow PEP 604 `X | None` union syntax to parse on Python 3.9 (CDK's venv).
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")


# ---- Configuration ----

@dataclass
class RenderBudget:
    """Resource limits for the Remotion render pipeline.

    These were previously scattered as magic numbers across render_stack.py,
    backend-node/remotion-invoker/index.js, and broll.py. Centralizing here
    so:
      - Changes are deliberate (rationale in-line with the value)
      - CDK + invoker + broll read from ONE source of truth
      - Tuning is visible (grep for RenderBudget to see every knob)
    """

    # Remotion Lambda (per-chunk renderer) — beefed up after chunks with
    # heavy b-roll timed out at 300s/2GB. Each chunk renders frames_per_chunk
    # frames; at 30fps, 150 frames = 5s of video per chunk.
    remotion_lambda_memory_mb: int = 3008     # more CPU = faster per frame
    remotion_lambda_disk_mb: int = 4096       # room for b-roll caching
    remotion_lambda_timeout_sec: int = 600    # 10 min ceiling per chunk
    frames_per_chunk: int = 150               # 5s chunks @ 30fps

    # Invoker Lambda (orchestrator, polls chunks until done).
    # Must be less than its own Lambda timeout of 15 min.
    invoker_poll_deadline_sec: int = 840      # 14 min

    # Input props budget — Remotion replicates inputProps across every chunk,
    # so large props + many chunks → 6MB Lambda response cap breaches
    # (Runtime.TruncatedResponse). Invoker warns if the slim payload drifts.
    input_props_max_bytes: int = 10_000       # ~10KB target

    # Per-reel Nova Reel shot cap — 30 Nova clips costs ~$14 and takes
    # forever. We only generate Nova for the primary shot of each beat,
    # but cap the total in case of prompt drift.
    max_nova_shots_per_reel: int = 15

    # Reel length — platforms reject >3 min.
    max_reel_duration_sec: int = 180          # YouTube Shorts / IG Reels

    # Step Functions overall execution budget.
    render_pipeline_max_duration_sec: int = 1500  # 25 min hard ceiling


@dataclass
class GuardrailsConfig:
    """Thresholds for a single pipeline run. Defaults tuned for a low-cost
    podcast workflow (~$5 budget per episode including Nova Reel)."""

    # -- Shared run budget --
    max_llm_calls_per_run: int = 20
    max_retry_budget_per_run: int = 8  # total retries across ALL steps
    max_estimated_cost_per_run: float = 8.00  # USD
    max_tokens_per_run: int = 500_000  # soft limit, logged not enforced

    # -- Per-step limits --
    max_retries_per_call: int = 2  # transient failures only
    timeout_per_step_sec: float = 180.0  # 3 minutes
    max_steps_per_run: int = 30  # total step invocations (including retries)

    # -- Circuit breaker --
    breaker_consecutive_failures: int = 3
    breaker_rolling_window_sec: float = 300.0  # 5 min
    breaker_rolling_failure_rate: float = 0.5  # 50% in rolling window
    breaker_cooldown_sec: float = 60.0

    # -- Loop prevention --
    max_identical_outputs: int = 4
    max_schema_repair_attempts: int = 2  # structured_output retries

    # -- Render pipeline (Remotion + Nova + reel length) --
    render: RenderBudget = field(default_factory=RenderBudget)


# ---- Exceptions ----

class GuardrailError(Exception):
    """Base for all guardrail-triggered aborts."""


class BudgetExceeded(GuardrailError):
    pass


class CircuitOpen(GuardrailError):
    pass


class StallDetected(GuardrailError):
    pass


class StepTimeout(GuardrailError):
    pass


class SchemaRepairExhausted(GuardrailError):
    pass


# ---- Circuit Breaker ----

@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    recent_results: list = field(default_factory=list)  # (timestamp, success: bool)
    open_until: float = 0.0


class CircuitBreaker:
    """Per-model circuit breaker. Opens after N consecutive failures or a
    high rolling failure rate."""

    def __init__(self, config: GuardrailsConfig):
        self.config = config
        self._models: dict[str, _BreakerState] = {}

    def _state(self, model: str) -> _BreakerState:
        if model not in self._models:
            self._models[model] = _BreakerState()
        return self._models[model]

    def check(self, model: str) -> None:
        s = self._state(model)
        if time.time() < s.open_until:
            raise CircuitOpen(
                f"Circuit open for {model} until "
                f"{s.open_until - time.time():.0f}s from now"
            )

    def record_success(self, model: str) -> None:
        s = self._state(model)
        s.consecutive_failures = 0
        s.recent_results.append((time.time(), True))
        self._trim(s)

    def record_failure(self, model: str, reason: str = "") -> None:
        s = self._state(model)
        s.consecutive_failures += 1
        s.recent_results.append((time.time(), False))
        self._trim(s)
        log(f"[breaker] {model} failure #{s.consecutive_failures}: {reason}")

        if s.consecutive_failures >= self.config.breaker_consecutive_failures:
            self._open(s, model, "consecutive failures")
            return
        # Check rolling failure rate.
        failures = sum(1 for _, ok in s.recent_results if not ok)
        total = len(s.recent_results)
        if total >= 4 and failures / total >= self.config.breaker_rolling_failure_rate:
            self._open(s, model, f"rolling rate {failures}/{total}")

    def _open(self, s: _BreakerState, model: str, reason: str) -> None:
        s.open_until = time.time() + self.config.breaker_cooldown_sec
        log(f"[breaker] OPEN for {model}: {reason}. Cooldown {self.config.breaker_cooldown_sec}s")

    def _trim(self, s: _BreakerState) -> None:
        cutoff = time.time() - self.config.breaker_rolling_window_sec
        s.recent_results = [(t, ok) for t, ok in s.recent_results if t > cutoff]


# ---- Logging ----

_run_logs: list[dict[str, Any]] = []


def log(msg: str, **extra: Any) -> None:
    """Structured log line. Printed to stdout (→ CloudWatch) and buffered."""
    entry = {"ts": time.time(), "msg": msg, **extra}
    _run_logs.append(entry)
    parts = [msg] + [f"{k}={v}" for k, v in extra.items()]
    print(" | ".join(parts))


def get_run_logs() -> list[dict[str, Any]]:
    return list(_run_logs)


# ---- Run Context ----

class RunContext:
    """Tracks budgets, breakers, and output hashes across all steps in one run."""

    def __init__(self, config: GuardrailsConfig | None = None):
        self.config = config or GuardrailsConfig()
        self.breaker = CircuitBreaker(self.config)
        self.llm_calls = 0
        self.total_retries = 0
        self.total_steps = 0
        self.estimated_cost = 0.0
        self.estimated_tokens = 0
        self.start_time = time.time()
        self._output_hashes: list[str] = []

    # -- Budget checks --

    def _check_budgets(self, step: str, estimated_cost: float = 0.0) -> None:
        if self.llm_calls >= self.config.max_llm_calls_per_run:
            raise BudgetExceeded(
                f"LLM call limit ({self.config.max_llm_calls_per_run}) "
                f"reached at step '{step}'"
            )
        if self.total_steps >= self.config.max_steps_per_run:
            raise BudgetExceeded(
                f"Step limit ({self.config.max_steps_per_run}) reached"
            )
        projected = self.estimated_cost + estimated_cost
        if projected > self.config.max_estimated_cost_per_run:
            raise BudgetExceeded(
                f"Projected cost ${projected:.2f} exceeds budget "
                f"${self.config.max_estimated_cost_per_run:.2f} at step '{step}'"
            )

    # -- Loop detection --

    def _check_stall(self, output: Any) -> None:
        h = hashlib.md5(
            json.dumps(output, sort_keys=True, default=str).encode()
        ).hexdigest()
        count = self._output_hashes.count(h)
        self._output_hashes.append(h)
        if count >= self.config.max_identical_outputs - 1:
            raise StallDetected(
                f"Identical output detected {count + 1} times — pipeline stalled"
            )

    # -- Main call wrapper --

    def call(
        self,
        step: str,
        fn: Callable[..., T],
        *args: Any,
        model: str = "unknown",
        estimated_cost: float = 0.0,
        estimated_tokens: int = 0,
        is_llm: bool = True,
        **kwargs: Any,
    ) -> T:
        """Execute a pipeline step with full guardrails.

        Handles: budget check → breaker check → timeout → retry (transient only)
                 → stall detection → cost/token tracking → logging.
        """
        self._check_budgets(step, estimated_cost)
        if is_llm:
            self.breaker.check(model)

        self.total_steps += 1
        attempt = 0
        last_error = None

        while attempt <= self.config.max_retries_per_call:
            attempt += 1
            t0 = time.time()
            try:
                log(
                    f"[step] {step}",
                    model=model,
                    attempt=attempt,
                    est_cost=f"${estimated_cost:.3f}",
                    est_tokens=estimated_tokens,
                    budget_spent=f"${self.estimated_cost:.2f}",
                    llm_calls=self.llm_calls,
                )
                result = fn(*args, **kwargs)
                elapsed = time.time() - t0

                # Success path.
                if is_llm:
                    self.llm_calls += 1
                    self.estimated_cost += estimated_cost
                    self.estimated_tokens += estimated_tokens
                    self.breaker.record_success(model)
                self._check_stall(result)
                log(
                    f"[step] {step} OK",
                    elapsed=f"{elapsed:.1f}s",
                    total_cost=f"${self.estimated_cost:.2f}",
                )
                return result

            except (BudgetExceeded, CircuitOpen, StallDetected, SchemaRepairExhausted):
                raise  # non-retryable guardrail errors
            except StepTimeout:
                raise
            except Exception as e:
                elapsed = time.time() - t0
                last_error = e
                is_transient = _is_transient(e)

                if is_llm:
                    self.breaker.record_failure(model, str(e)[:200])

                if not is_transient or attempt > self.config.max_retries_per_call:
                    log(
                        f"[step] {step} FAILED (non-retryable)",
                        error=str(e)[:300],
                        elapsed=f"{elapsed:.1f}s",
                        attempt=attempt,
                    )
                    raise

                self.total_retries += 1
                if self.total_retries > self.config.max_retry_budget_per_run:
                    raise BudgetExceeded(
                        f"Retry budget ({self.config.max_retry_budget_per_run}) exhausted"
                    ) from e

                wait = min(2 ** attempt, 30)
                log(
                    f"[step] {step} transient error, retrying in {wait}s",
                    error=str(e)[:200],
                    attempt=attempt,
                )
                time.sleep(wait)

        raise last_error  # type: ignore

    def summary(self) -> dict[str, Any]:
        """End-of-run summary for logging."""
        return {
            "llm_calls": self.llm_calls,
            "total_steps": self.total_steps,
            "total_retries": self.total_retries,
            "estimated_cost": f"${self.estimated_cost:.2f}",
            "estimated_tokens": self.estimated_tokens,
            "elapsed": f"{time.time() - self.start_time:.1f}s",
        }


# ---- Helpers ----

def _is_transient(e: Exception) -> bool:
    """Heuristic: is this error worth retrying?"""
    name = type(e).__name__
    msg = str(e).lower()
    # Transient: throttling, timeouts, 5xx, connection errors.
    transient_patterns = [
        "throttl", "too many requests", "timed out", "timeout",
        "connection", "503", "502", "429", "internal server error",
        "service unavailable", "read timeout", "connect timeout",
    ]
    if any(p in msg for p in transient_patterns):
        return True
    if name in ("ThrottlingException", "ReadTimeoutError", "ConnectTimeoutError",
                "ServiceUnavailableException", "TooManyRequestsException"):
        return True
    return False
