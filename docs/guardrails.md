# Guardrails — Level 300 Reference

> **Audience**: Engineers who are extending the pipeline, tuning limits, or
> debugging a failed run. Assumes familiarity with AWS Lambda, Bedrock, and
> Step Functions.

---

## Why guardrails exist

The Vyas-Video pipeline makes multiple Bedrock calls and fires up to 15
Amazon Nova Reel jobs per short. Without explicit controls:

- A single bad run could exhaust a monthly Bedrock budget
- A flaky model could retry indefinitely, burning Lambda time
- A Nova Reel throttle storm could block the broll Lambda until it times out
- A silently stalled LLM (returning identical outputs) would waste retries
  with no signal

`backend/guardrails.py` provides a single Python class — `RunContext` — that
every pipeline step routes through. It is **not** a Bedrock feature; it is pure
Python code that wraps function calls.

---

## Architecture

```
Pipeline step (e.g. screenwriter)
        │
        ▼
ctx.call("step_name", fn, *args, estimated_cost=0.04, model="sonnet-4.6")
        │
        ├─ _check_budgets()        ← run-wide cost / call / step caps
        ├─ breaker.check(model)    ← per-model circuit breaker
        │
        │   ┌── retry loop (≤ max_retries_per_call) ──────────────────────┐
        │   │                                                              │
        │   ├─ log("[step] start")                                        │
        │   ├─ fn(*args)           ← actual Bedrock / ffmpeg / S3 call   │
        │   │                                                              │
        │   │  on success:                                                 │
        │   │   ├─ record_success(model)                                  │
        │   │   ├─ _check_stall(result)                                   │
        │   │   ├─ log("[step] OK")                                       │
        │   │   └─ return result                                           │
        │   │                                                              │
        │   │  on exception:                                               │
        │   │   ├─ record_failure(model)                                  │
        │   │   ├─ if non-retryable → raise immediately                  │
        │   │   ├─ if retry budget exhausted → raise BudgetExceeded      │
        │   │   └─ else → sleep(2^attempt, max 30s) → retry              │
        │   └──────────────────────────────────────────────────────────────┘
        │
        └─ raises GuardrailError subclass on any limit breach
```

---

## Configuration (`GuardrailsConfig`)

All limits live in `GuardrailsConfig` (dataclass). The defaults are tuned for
a low-cost podcast workflow:

```python
@dataclass
class GuardrailsConfig:
    # Run-wide budgets
    max_llm_calls_per_run: int = 20
    max_retry_budget_per_run: int = 8    # total retries across ALL steps
    max_estimated_cost_per_run: float = 8.00   # USD
    max_tokens_per_run: int = 500_000    # soft — logged, not enforced

    # Per-step limits
    max_retries_per_call: int = 2
    timeout_per_step_sec: float = 180.0
    max_steps_per_run: int = 30

    # Circuit breaker
    breaker_consecutive_failures: int = 3
    breaker_rolling_window_sec: float = 300.0   # 5 min
    breaker_rolling_failure_rate: float = 0.5   # 50%
    breaker_cooldown_sec: float = 60.0

    # Loop prevention
    max_identical_outputs: int = 4
    max_schema_repair_attempts: int = 2

    # Render pipeline (Remotion + Nova Reel)
    render: RenderBudget = field(default_factory=RenderBudget)
```

### How to override for a specific Lambda

The broll Lambda needs higher limits because Nova Reel jobs are expensive and
numerous. It creates its own `RunContext` with custom config:

```python
budget = RenderBudget()
ctx = RunContext(
    GuardrailsConfig(
        max_llm_calls_per_run=budget.max_nova_shots_per_reel * 3 + 10,
        max_estimated_cost_per_run=budget.max_nova_shots_per_reel * 0.48 * 3 + 5.0,
    )
)
```

At 15 shots: `max_llm_calls = 55`, `max_cost = $26.60`. The wider budget
reflects that Nova is the dominant cost driver and the default $8 cap would
fire before all shots are started.

---

## Budget scope: per Lambda invocation, not per episode

One `RunContext` is created per Lambda invocation and passed to every step
within that invocation. There is **no shared state across Lambda boundaries**.

| Lambda invocation | Its RunContext | Budget resets? |
|---|---|---|
| API Lambda — ideation | 1 ctx | Fresh per invocation |
| API Lambda — script gen | 1 ctx | Fresh per invocation |
| Broll Lambda | 1 ctx (custom) | Fresh per invocation |
| Pack Lambda | No ctx | No LLM calls |

Consequence: a single episode's full end-to-end cost (ideation + script + broll)
is not tracked in one place. Each Lambda invocation enforces its own cap. If
you need cross-invocation budgeting, track it externally (e.g. DynamoDB cost
field updated at end of each Lambda, checked at start of the next).

---

## Cost estimation: declared, not metered

`estimated_cost` is **hardcoded by the developer** at each call site — it is
not read from Bedrock's response:

```python
ctx.call(
    "short.screenwriter", write_script, idea, timed,
    model="sonnet-4.6",
    estimated_cost=0.04,     # ← developer estimate, not measured
    estimated_tokens=20000,
)
```

The developer derives this from Bedrock's published pricing × typical prompt
size. Actual Bedrock billing happens regardless of what `estimated_cost` says.

**Implication**: if a prompt grows (e.g. longer transcript), the declared
estimate stays fixed while real cost rises. For variable-length inputs, call
Bedrock's `count_tokens` API before the step and pass the real value in.

The budget check is a **planning fence**:

```python
projected = self.estimated_cost + estimated_cost
if projected > self.config.max_estimated_cost_per_run:
    raise BudgetExceeded(...)   # fn() is never called
```

It fires *before* the actual call, so no money is spent on the blocked step.

---

## Transient error retries

`_is_transient(e)` classifies exceptions into retryable vs non-retryable:

**Retryable (transient):**
- HTTP 429, 503, 502 — service throttling or temporary unavailability
- `ThrottlingException`, `ServiceUnavailableException` — Bedrock rate limits
- `ReadTimeoutError`, `ConnectTimeoutError` — network blips

**Non-retryable:**
- HTTP 400 — bad request (your prompt or params are wrong; retrying won't help)
- `ValidationException` — malformed input
- JSON parse failures — model returned garbage; retry risks stall detection
- All `GuardrailError` subclasses — never retried

**Retry mechanics:**

```python
wait = min(2 ** attempt, 30)   # attempt 1 → 2s, attempt 2 → 4s, capped at 30s
```

**Two independent retry limits — easy to confuse:**

| Limit | Scope | What happens when exhausted |
|---|---|---|
| `max_retries_per_call = 2` | Single step | That step raises immediately |
| `max_retry_budget_per_run = 8` | Entire run | `BudgetExceeded` raised, run aborts |

Example: if step A retried twice (2 of 8 spent) and step B retried twice (4 of 8
spent), step C has 4 retries left before the run-wide budget fires. This
prevents a persistently flaky model from silently consuming all retry capacity
and leaving downstream steps with no resilience.

---

## Circuit breaker

The circuit breaker is **per model** (keyed on the model string passed to
`ctx.call()`). It opens under two conditions:

| Trigger | Threshold |
|---|---|
| Consecutive failures | 3 in a row |
| Rolling failure rate | ≥ 50% of calls within the last 5 minutes |

When open, all calls to that model raise `CircuitOpen` immediately (no network
call made) until the cooldown period (60s) expires.

**Why per-model, not per-step?** A single model failure mode (e.g. Haiku
being throttled across a region) should block *all* uses of that model, not
just the step that first encountered the error. This prevents every step from
independently burning retries against the same broken endpoint.

The breaker state lives in memory (the `RunContext` object). It does not
persist across Lambda invocations — each cold start begins with a clean breaker.

---

## Stall detection (loop prevention)

After every **successful** call, `_check_stall()` hashes the result with MD5
and compares it against all previous outputs in the run:

```python
h = hashlib.md5(json.dumps(output, sort_keys=True, default=str).encode()).hexdigest()
count = self._output_hashes.count(h)
self._output_hashes.append(h)
if count >= self.config.max_identical_outputs - 1:   # default: 3 identical → abort
    raise StallDetected(...)
```

This is **not** for agentic loops. It guards against a specific failure mode
in a deterministic pipeline: a model returning the same malformed-but-parseable
output on every retry (e.g. a screenplay with only one beat regardless of input),
which would keep succeeding from the guardrail's perspective while producing
useless results.

The check runs on the success path because failed calls are already handled by
the retry logic. The concern is subtler: a call that returns 200 OK but always
returns the same thing.

---

## RenderBudget (`guardrails.py`)

`RenderBudget` centralizes all Remotion + Nova Reel thresholds that were
previously scattered as magic numbers across `render_stack.py`, the invoker,
and `broll.py`. Every knob is in one place with rationale inline.

| Field | Value | Rationale |
|---|---|---|
| `remotion_lambda_memory_mb` | 3008 | More vCPU allocation → faster frame render |
| `remotion_lambda_disk_mb` | 4096 | B-roll video files cached to disk during render |
| `remotion_lambda_timeout_sec` | 600 | 10 min ceiling per chunk; raised after heavy b-roll chunks timed out at 300s |
| `frames_per_chunk` | 150 | 5s @ 30fps — balances chunk count vs per-chunk memory |
| `invoker_poll_deadline_sec` | 840 | 14 min — must be less than the invoker Lambda's 15 min timeout |
| `input_props_max_bytes` | 10,000 | Remotion replicates inputProps across every chunk; >~10 KB × many chunks hits the 6 MB Lambda response cap (`Runtime.TruncatedResponse`) |
| `max_nova_shots_per_reel` | 15 | Nova rate limit: ~3 starts per 2 min rolling window. >15 shots cannot reliably start within a 15 min Lambda; shot trimming keeps reels under this cap |
| `max_reel_duration_sec` | 180 | YouTube Shorts / Instagram Reels platform limit |
| `render_pipeline_max_duration_sec` | 1500 | 25 min Step Functions hard ceiling |

---

## Observability: where logs go

Every `log()` call does `print()` → Lambda stdout → **CloudWatch Logs**.

| Lambda | CloudWatch log group |
|---|---|
| API (ideation, script gen, rerender) | `/aws/lambda/VyasVideoApi-ApiFnE0725F78-KDyPYiZCmAqE` |
| Broll (Nova Reel) | `/aws/lambda/VyasVideoRender-BrollFnD20584D6-6RSoI0H2aQsa` |

Both log groups are in AWS account **568838249405** (`kostops-payer` profile),
`us-east-1`. This is the same account where all Vyas-Video resources run —
not a separate billing/payer account.

**Log format** (pipe-delimited key=value):

```
[step] short.screenwriter | model=sonnet-4.6 | attempt=1 | est_cost=$0.040 | est_tokens=20000 | budget_spent=$0.00 | llm_calls=0
[step] short.screenwriter OK | elapsed=12.3s | total_cost=$0.04
[step] short.director | model=haiku-4.5 | attempt=1 | est_cost=$0.005 | est_tokens=5000 | budget_spent=$0.04 | llm_calls=1
[step] short.director OK | elapsed=4.1s | total_cost=$0.05
[breaker] haiku-4.5 failure #1: ThrottlingException
[step] short.director transient error, retrying in 2s | error=ThrottlingException | attempt=1
[step] short.audio_slice OK | elapsed=1.5s | total_cost=$0.05
```

**Query last hour of guardrail events for API Lambda:**

```bash
AWS_PROFILE=kostops-payer aws logs filter-log-events \
  --log-group-name "/aws/lambda/VyasVideoApi-ApiFnE0725F78-KDyPYiZCmAqE" \
  --start-time $(python3 -c "import time; print(int((time.time()-3600)*1000))") \
  --filter-pattern '"[step]"' \
  --region us-east-1 \
  --query 'events[*].message' --output text
```

**Query broll Lambda for Nova start/complete events:**

```bash
AWS_PROFILE=kostops-payer aws logs filter-log-events \
  --log-group-name "/aws/lambda/VyasVideoRender-BrollFnD20584D6-6RSoI0H2aQsa" \
  --start-time $(python3 -c "import time; print(int((time.time()-3600)*1000))") \
  --filter-pattern '"[broll]"' \
  --region us-east-1 \
  --query 'events[*].message' --output text
```

---

## Exception hierarchy

```
GuardrailError          ← base; always fatal (never retried)
  ├── BudgetExceeded    ← run cost / call count / step count / retry budget
  ├── CircuitOpen       ← model circuit breaker is open
  ├── StallDetected     ← identical output appeared max_identical_outputs times
  ├── StepTimeout       ← per-step wall-clock timeout exceeded
  └── SchemaRepairExhausted ← structured output could not be parsed after max_schema_repair_attempts retries
```

Non-`GuardrailError` exceptions from `fn()` are classified by `_is_transient()`
and either retried or re-raised as-is.

---

## Tuning guide

| Symptom | Likely cause | Knob to change |
|---|---|---|
| Run aborts with `BudgetExceeded: LLM call limit` | Pipeline has more steps than expected | `max_llm_calls_per_run` |
| Run aborts with `BudgetExceeded: Projected cost $X` | Step cost estimates are stale or inputs grew | `max_estimated_cost_per_run` or update `estimated_cost` at call site |
| Run aborts with `BudgetExceeded: Retry budget exhausted` | One model is persistently throttled | `max_retry_budget_per_run` or check Bedrock quota |
| `CircuitOpen` on cold start | Breaker opened in a prior run but doesn't persist | Won't happen — breaker resets per invocation; if reproducible, investigate the model error |
| Broll Lambda times out before all Nova shots start | Too many shots for batch cadence | Reduce `max_nova_shots_per_reel` or widen `_NOVA_BATCH_DELAY` |
| `Runtime.TruncatedResponse` in Remotion | inputProps too large | Strip non-essential fields from props; check invoker log for the size warning |
| `StallDetected` on a valid step | Model returning same output for different inputs | Investigate the prompt; add temperature or vary context |

---

## Known limits and gaps

- **Cost tracking is estimated, not actual** — real Bedrock spend can diverge if
  prompt sizes change without updating `estimated_cost` at the call site.
- **No cross-invocation budget** — each Lambda invocation has an independent
  `RunContext`; there is no global "this episode has spent $X" tracker across
  ideation + script + broll.
- **Breaker state is in-memory** — does not survive Lambda cold starts or
  persist between steps in different Lambdas.
- **Stall detection hashes the full output** — for large outputs (full
  screenplay JSON), this is cheap but means two near-identical outputs (one field
  different) are not detected as stalls.
- **Timeout (`timeout_per_step_sec`)** is defined in config but the actual
  timeout enforcement requires the caller to wrap `fn` in a thread with a
  join timeout. Not all call sites implement this — check before relying on it.
