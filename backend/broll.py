"""Step Functions handler: generate b-roll for each scene via Nova Reel.

100% Nova Reel — every clip is generated from the exact visual prompt so it
stays aligned with the voiceover. No stock footage (Pexels/generic) is used.

Failure handling (per shot, up to MAX_NOVA_ATTEMPTS attempts):
  - Content / safety filter  → rewrite prompt with Claude Haiku, new job
  - Timeout                  → retry same prompt (transient queue delay)
  - Generic failure           → retry same prompt once
  - Budget exhausted          → skip (logged as ERROR, renders black)
  - All attempts exhausted    → source: "nova-exhausted" (renders black; we
                                prefer a black gap over wrong/misaligned content)

After each successful clip, the shot scorer (Claude Haiku vision) checks
quality and audio-video alignment. If either is below threshold, one more
Nova job fires with an improved/voiceover-anchored prompt.
"""
import json
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import boto3

import nova_reel
from guardrails import RunContext, RenderBudget, GuardrailsConfig, log as glog
from agents.shot_scorer import score_shot, RETRY_THRESHOLD, ALIGNMENT_THRESHOLD

_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime")
BUCKET = os.environ["ASSETS_BUCKET"]

# Maximum Nova Reel attempts per shot (error-based retry).
MAX_NOVA_ATTEMPTS = int(os.environ.get("MAX_NOVA_ATTEMPTS", "3"))

# Model used to rewrite prompts that hit Nova's content/safety filter.
_REWRITE_MODEL = os.environ.get(
    "BEDROCK_DIRECTOR_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)


def _extract_frame(mp4_bytes: bytes, offset_sec: float = 3.0) -> bytes | None:
    """Extract a JPEG frame at offset_sec from mp4 bytes using ffmpeg.
    Returns None if extraction fails (non-fatal)."""
    import subprocess
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="shot-score-") as tmp:
            src = os.path.join(tmp, "clip.mp4")
            dst = os.path.join(tmp, "frame.jpg")
            with open(src, "wb") as f:
                f.write(mp4_bytes)
            result = subprocess.run(
                ["/opt/bin/ffmpeg", "-y", "-i", src,
                 "-ss", str(offset_sec), "-frames:v", "1", "-q:v", "3", dst],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(dst):
                with open(dst, "rb") as f:
                    return f.read()
            print(f"[shot_score] ffmpeg exit {result.returncode}: {result.stderr[-200:]!r}")
    except Exception as e:
        print(f"[shot_score] frame extract failed: {e!r}")
    return None


def _rewrite_safe_prompt(prompt: str) -> str:
    """Rewrite a Nova Reel prompt that triggered a content/safety filter.

    Uses Claude Haiku to strip policy-violating content while preserving
    the visual intent. Falls back to simple word substitution on error.
    Cost: ~$0.001.
    """
    try:
        resp = _bedrock.invoke_model(
            modelId=_REWRITE_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 200,
                "messages": [{
                    "role": "user",
                    "content": (
                        "This video generation prompt was rejected by a content safety filter. "
                        "Rewrite it to be safe for AI video generation while keeping the same "
                        "cinematic visual intent. Remove or rephrase anything that could trigger "
                        "safety filters (violence, explicit content, controversial figures, etc.). "
                        "Keep it literal, filmable, warm amber style.\n\n"
                        f"Original: {prompt}\n\n"
                        "Return ONLY the rewritten prompt, no explanation."
                    ),
                }],
            }),
        )
        rewritten = json.loads(resp["body"].read())["content"][0]["text"].strip()
        return rewritten if rewritten else prompt
    except Exception as e:
        print(f"[broll] prompt rewrite failed ({e!r}) — falling back to simple substitution")
        # Simple fallback: strip common filter trigger words.
        safe = prompt
        for word in ["death", "dead", "dying", "kill", "blood", "war", "violence",
                     "weapon", "gun", "naked", "nude", "sexual", "hate", "terror"]:
            safe = safe.replace(word, "peaceful")
        return safe


# Limit concurrent ffmpeg + scorer calls to avoid OOM.
# Each ffmpeg subprocess peaks at ~100 MB; 4 concurrent = ~400 MB headroom.
# Polling threads are unaffected — they acquire this only during Phase B.
_SCORER_SEM = threading.Semaphore(4)


def _presign(key: str) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 2,
    )


def _flatten_shots(script: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten beats→shots into a flat list with global IDs for Nova Reel.
    Each entry has: global_id, beat_idx, shot_idx, shot dict, beat purpose."""
    flat: list[dict[str, Any]] = []
    beats = script.get("beats") or []
    # Fallback: legacy scenes[] treated as single-shot beats.
    if not beats:
        for i, scene in enumerate(script.get("scenes") or []):
            flat.append({
                "global_id": f"b{i}_s0",
                "beat_idx": i,
                "shot_idx": 0,
                "visual": scene.get("visual", ""),
                "beat_purpose": scene.get("beat_type") or scene.get("purpose", "build"),
                "broll_queries": scene.get("broll_queries", []),
                "broll_query": scene.get("broll_query", ""),
                "shot_duration_sec": float(scene.get("end", 0)) - float(scene.get("start", 0)),
            })
        return flat
    for bi, beat in enumerate(beats):
        for si, shot in enumerate(beat.get("shots") or []):
            flat.append({
                "global_id": f"b{bi}_s{si}",
                "beat_idx": bi,
                "shot_idx": si,
                "visual": shot.get("visual", ""),
                "voiceover": beat.get("voiceover", ""),
                "beat_purpose": beat.get("purpose", "build"),
                "shot_role": shot.get("shot_role", "establish"),
                "broll_queries": shot.get("broll_queries", []),
                "broll_query": shot.get("broll_query", ""),
                "shot_duration_sec": float(shot.get("shot_duration_sec", 3)),
            })
    return flat


def handler(event: dict[str, Any], _ctx) -> dict[str, Any]:
    # broll has its own budget envelope: up to 30 shots × 3 attempts × $0.48
    # = ~$43 worst-case, but realistic is 30 × $0.48 = $14.40 primary +
    # ~5 quality retries ≈ $17. Use a dedicated config so the general
    # GuardrailsConfig (tuned for LLM-heavy steps) doesn't cap Nova starts.
    budget = RenderBudget()
    ctx = RunContext(
        GuardrailsConfig(
            max_llm_calls_per_run=budget.max_nova_shots_per_reel * 3 + 10,  # 30 shots × 3 attempts + headroom
            max_estimated_cost_per_run=budget.max_nova_shots_per_reel * 0.48 * 3 + 5.0,  # ~$48 ceiling
        )
    )
    episode_id = event["episode_id"]
    idea_rank = event.get("idea_rank")
    project_id = event.get("project_id", f"{episode_id}/idea-{idea_rank}")
    script = json.loads(_s3.get_object(Bucket=BUCKET, Key=event["script_s3_key"])["Body"].read())

    shots = _flatten_shots(script)
    shot_broll: dict[str, dict[str, Any]] = {}

    # 100% Nova Reel — cap to guardrail, excess shots are logged as skipped.
    nova_shots = list(shots)
    if len(nova_shots) > budget.max_nova_shots_per_reel:
        demoted = nova_shots[budget.max_nova_shots_per_reel:]
        nova_shots = nova_shots[:budget.max_nova_shots_per_reel]
        for shot in demoted:
            gid = shot["global_id"]
            glog(
                f"[broll] guardrail: {gid} exceeds cap "
                f"({budget.max_nova_shots_per_reel}) — will render black"
            )
            shot_broll[gid] = {
                "global_id": gid, "broll_key": None, "broll_url": None,
                "source": "cap-exceeded",
            }

    glog(f"[broll] {len(shots)} total shots → {len(nova_shots)} Nova Reel jobs")

    # Start all Nova jobs in rate-limit-aware batches.
    # Nova Reel allows ~3 starts per 30-second rolling window. We submit in
    # batches of _NOVA_BATCH_SIZE with a 30s pause between batches so we never
    # exhaust the window. All started jobs run concurrently on Nova's side;
    # we poll them all in parallel below so total wait ≈ slowest single job.
    _NOVA_BATCH_SIZE = 3     # starts per rate-limit window (Nova's quota)
    _NOVA_BATCH_DELAY = 45   # seconds between batches (Nova window ~120s)
    _NOVA_INTRA_DELAY = 1    # seconds between starts within a batch

    # pending maps gid → (arn, nova_prefix, shot_dict)
    pending: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for j, shot in enumerate(nova_shots):
        if j > 0:
            if j % _NOVA_BATCH_SIZE == 0:
                glog(f"[broll] batch {j // _NOVA_BATCH_SIZE} complete — waiting {_NOVA_BATCH_DELAY}s for rate limit window")
                _time.sleep(_NOVA_BATCH_DELAY)
            else:
                _time.sleep(_NOVA_INTRA_DELAY)
        gid = shot["global_id"]
        prompt_text = shot["visual"] or "A cinematic warm amber light slow motion scene"
        nova_prefix = f"tmp/nova/{project_id}/{gid}"
        try:
            ctx._check_budgets(f"nova.{gid}", estimated_cost=0.48)
            arn = nova_reel.start(
                prompt_text, BUCKET, nova_prefix,
                beat_type=shot.get("beat_purpose", "build"),
            )
            ctx.estimated_cost += 0.48
            ctx.llm_calls += 1
            pending[gid] = (arn, nova_prefix, shot)
            glog(f"[broll] nova start {gid}", cost=f"${ctx.estimated_cost:.2f}")
        except Exception as e:
            glog(f"[broll] nova start failed {gid}: {e!r}")
            shot_broll[gid] = {
                "global_id": gid, "broll_key": None, "broll_url": None,
                "source": "nova-start-failed",
            }

    # Poll all Nova jobs concurrently, with per-shot error-based retry.
    def _wait_and_copy(gid: str) -> dict[str, Any]:
        arn, initial_prefix, shot = pending[gid]
        broll_key = f"projects/{project_id}/broll/{gid}.mp4"

        current_arn = arn
        current_prompt = shot["visual"]

        # ── Pass A: Error-based retry loop ───────────────────────────────────
        for attempt in range(1, MAX_NOVA_ATTEMPTS + 1):
            try:
                resp = nova_reel.wait(current_arn, timeout_sec=360)
                nova_key = nova_reel.output_key(resp)
                _s3.copy_object(
                    Bucket=BUCKET, Key=broll_key,
                    CopySource={"Bucket": BUCKET, "Key": nova_key},
                )
                glog(f"[broll] nova complete {gid} (attempt {attempt})")
                break  # clip is on S3 — move to scoring

            except (TimeoutError, RuntimeError) as e:
                err_str = str(e).lower()
                glog(f"[broll] nova attempt {attempt}/{MAX_NOVA_ATTEMPTS} failed {gid}: {e!r}")

                if attempt == MAX_NOVA_ATTEMPTS:
                    glog(
                        f"[broll] ERROR: {gid} exhausted {MAX_NOVA_ATTEMPTS} Nova attempts "
                        f"— will render as BLACK (refusing generic/stock fallback)"
                    )
                    return {
                        "global_id": gid, "broll_key": None, "broll_url": None,
                        "source": "nova-exhausted",
                    }

                # Decide retry strategy based on error message.
                is_content_filter = any(
                    kw in err_str
                    for kw in ["content", "policy", "moderat", "filter", "safety",
                               "inappropriate", "harmful"]
                )
                is_timeout = isinstance(e, TimeoutError) or "timeout" in err_str

                if is_content_filter:
                    glog(
                        f"[broll] {gid} attempt {attempt}: content/safety filter — "
                        f"rewriting prompt with Haiku"
                    )
                    current_prompt = _rewrite_safe_prompt(current_prompt)
                elif is_timeout:
                    glog(
                        f"[broll] {gid} attempt {attempt}: timeout — "
                        f"retrying same prompt (transient)"
                    )
                    _time.sleep(5)
                else:
                    glog(
                        f"[broll] {gid} attempt {attempt}: generic failure — "
                        f"retrying same prompt"
                    )
                    _time.sleep(10)

                # Start a fresh Nova job for the next attempt.
                try:
                    ctx._check_budgets(f"nova.{gid}.retry{attempt}", estimated_cost=0.48)
                    retry_prefix = f"tmp/nova/{project_id}/{gid}-retry{attempt}"
                    current_arn = nova_reel.start(
                        current_prompt, BUCKET, retry_prefix,
                        beat_type=shot.get("beat_purpose", "build"),
                    )
                    ctx.estimated_cost += 0.48
                except Exception as start_e:
                    glog(f"[broll] {gid} retry start failed: {start_e!r}")
                    return {
                        "global_id": gid, "broll_key": None, "broll_url": None,
                        "source": "nova-exhausted",
                    }

        # ── Pass B: Quality + Alignment gate ────────────────────────────────
        # Score the clip; fire one more Nova job if quality or alignment is low.
        # Acquire semaphore before downloading + running ffmpeg to cap concurrent
        # memory usage (each ffmpeg subprocess ~100 MB peak).
        if RETRY_THRESHOLD > 0 or ALIGNMENT_THRESHOLD > 0:
            try:
                with _SCORER_SEM:
                    mp4_bytes = _s3.get_object(Bucket=BUCKET, Key=broll_key)["Body"].read()
                    frame = _extract_frame(mp4_bytes)
                if frame:
                    scored = score_shot(frame, shot["visual"], shot.get("voiceover", ""))
                    score_val = scored.get("score", 7)
                    align_val = scored.get("alignment_score", 7)
                    glog(
                        f"[shot_score] {gid} "
                        f"quality={score_val}/10 alignment={align_val}/10 "
                        f"| {scored.get('feedback', '')} "
                        f"| align: {scored.get('alignment_note', '')}"
                    )

                    retry_reason: str | None = None
                    retry_prompt: str | None = None

                    if ALIGNMENT_THRESHOLD > 0 and align_val < ALIGNMENT_THRESHOLD:
                        retry_reason = f"alignment={align_val} < {ALIGNMENT_THRESHOLD}"
                        retry_prompt = (
                            scored.get("alignment_improved_prompt")
                            or scored.get("improved_prompt")
                            or shot["visual"]
                        )
                    elif RETRY_THRESHOLD > 0 and score_val < RETRY_THRESHOLD:
                        retry_reason = f"quality={score_val} < {RETRY_THRESHOLD}"
                        retry_prompt = scored.get("improved_prompt") or shot["visual"]

                    if retry_reason and retry_prompt:
                        glog(f"[shot_score] {gid} {retry_reason} → quality retry")
                        try:
                            ctx._check_budgets(f"nova.{gid}.quality-retry", estimated_cost=0.48)
                            qr_prefix = f"tmp/nova/{project_id}/{gid}-qretry"
                            qr_arn = nova_reel.start(
                                retry_prompt, BUCKET, qr_prefix,
                                beat_type=shot.get("beat_purpose", "build"),
                                max_retries=2,
                            )
                            ctx.estimated_cost += 0.48
                            qr_resp = nova_reel.wait(qr_arn, timeout_sec=180)
                            qr_key = nova_reel.output_key(qr_resp)
                            _s3.copy_object(
                                Bucket=BUCKET, Key=broll_key,
                                CopySource={"Bucket": BUCKET, "Key": qr_key},
                            )
                            retry_source = (
                                "nova-reel-retry-alignment"
                                if "alignment" in retry_reason
                                else "nova-reel-retry"
                            )
                            glog(f"[shot_score] {gid} quality retry complete ({retry_source})")
                            return {
                                "global_id": gid,
                                "broll_key": broll_key,
                                "broll_url": _presign(broll_key),
                                "source": retry_source,
                            }
                        except Exception as qr_e:
                            glog(f"[shot_score] {gid} quality retry failed (non-fatal): {qr_e!r}")
            except Exception as score_e:
                glog(f"[shot_score] {gid} scoring skipped (non-fatal): {score_e!r}")

        return {
            "global_id": gid,
            "broll_key": broll_key,
            "broll_url": _presign(broll_key),
            "source": "nova-reel",
        }

    if pending:
        # All Nova jobs were already started — they run concurrently in the cloud.
        # Use one worker per pending job so we poll all results simultaneously.
        # Total wait ≈ slowest single job (~5 min) instead of ceil(N/6) × 5 min.
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futures = {pool.submit(_wait_and_copy, gid): gid for gid in pending}
            for fut in as_completed(futures):
                result = fut.result()
                shot_broll[result["global_id"]] = result

    # Final audit — log any shots that have no clip so the gap is visible in
    # CloudWatch rather than silently rendering as black.
    missing = [
        s["global_id"] for s in shots
        if not shot_broll.get(s["global_id"], {}).get("broll_key")
    ]
    if missing:
        glog(
            f"[broll] ERROR: {len(missing)} shot(s) have no clip and will render as BLACK: "
            f"{missing}"
        )

    broll_list = [
        shot_broll.get(s["global_id"], {"global_id": s["global_id"], "source": "none"})
        for s in shots
    ]
    return {**event, "shot_broll": broll_list, "scene_broll": broll_list}
