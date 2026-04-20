# Vyas-Video — Architecture

## Goal

Take a long-form spiritual podcast (Bhagavad Gita commentary) and produce short-form reels (≤3 min) that:
- Use the **host's real voice** (no TTS in the happy path)
- Feature **cinematic AI-generated b-roll** matched to each spoken beat
- Have a **complete narrative arc** (hook → setup → build → twist → payoff)
- Are **uploadable directly** to YouTube Shorts and Instagram Reels

Audience: 15-35 year olds globally. Modern, relatable tone, not a sermon.

---

## Top-level shape

```
┌───────────────────┐       ┌────────────────────────────────────────────┐
│  Next.js frontend │       │            AWS (us-east-1)                  │
│  CloudFront + S3  │       │  ┌─────────────────┐   ┌─────────────────┐ │
│                   │       │  │ Cognito Auth    │   │ AWS Transcribe  │ │
│  ┌──────────────┐ │       │  │ (ID tokens)     │   │ (async)         │ │
│  │ AuthGate     │ │       │  └─────────────────┘   └─────────────────┘ │
│  │ Amplify v6   │◀┼───────┼──▶│ API Gateway (HTTP v2) + JWT authorizer │
│  └──────────────┘ │       │  └─────────────────┘                        │
│                   │       │           │                                  │
│  ┌──────────────┐ │       │           ▼                                  │
│  │ Sidebar +    │◀┼───────┼──▶┌─────────────────┐ async self-invoke     │
│  │ Idea/Beat UI │ │       │   │  API Lambda     │◀───────────┐           │
│  └──────────────┘ │       │   │  (FastAPI +     │            │           │
└───────────────────┘       │   │   Mangum +      │ script/    │           │
                            │   │   Strands)      │ ideate     │           │
                            │   │                 │ workers    │           │
                            │   └────────┬────────┘            │           │
                            │            │                      │           │
                            │  ┌─────────▼─────────┐           │           │
                            │  │  Bedrock          │           │           │
                            │  │  Opus 4.6         │           │           │
                            │  │  Sonnet 4.6       │           │           │
                            │  │  Haiku 4.5        │           │           │
                            │  │  Nova Reel v1:1   │           │           │
                            │  └───────────────────┘           │           │
                            │            │                      │           │
                            │  ┌─────────▼─────────┐           │           │
                            │  │  S3 assets bucket │           │           │
                            │  │  (episodes/...)    │           │           │
                            │  └─────────┬─────────┘           │           │
                            │            │                      │           │
                            │  ┌─────────▼─────────┐           │           │
                            │  │  DynamoDB         │           │           │
                            │  │  (episodes/ideas/ │           │           │
                            │  │   scripts/renders)│           │           │
                            │  └───────────────────┘           │           │
                            │                                    │           │
                            │  ┌──────────────────────────────┐ │           │
                            │  │  Step Functions              │ │           │
                            │  │  ┌─────┐ ┌──────┐ ┌───────┐ │ │           │
                            │  │  │Broll│→│Remote│→│ Pack  │ │ │           │
                            │  │  │     │ │-ion  │ │       │ │ │           │
                            │  │  └─────┘ └──────┘ └───────┘ │ │           │
                            │  └──────────────────────────────┘ │           │
                            └────────────────────────────────────┘           │
                                                                             │
                            (API Lambda invokes itself async for long-running
                             tasks to bypass the 30s API Gateway timeout)    │
                                                                             ┘
```

---

## Stacks (AWS CDK)

| Stack | Purpose |
|---|---|
| `VyasVideoAuth` | Cognito User Pool (`vyas-video-users`) + web client |
| `VyasVideoRender` | Shared assets S3 bucket, DynamoDB table, Step Functions pipeline, broll/render/pack Lambdas |
| `VyasVideoApi` | FastAPI-on-Lambda for all HTTP routes, FFmpeg layer, JWT authorizer attached to API Gateway |
| `VyasVideoFrontend` | Private S3 + CloudFront for Next.js static export |

All resources tagged `app=vyas-video` for cost allocation.

---

## Data model (single DynamoDB table)

```
pk=EPISODE#<number>     sk=META                             episode metadata + transcribe status
pk=EPISODE#<number>     sk=IDEA#<rank>                      one idea (title, hook, twist, payoff, window)
pk=EPISODE#<number>     sk=IDEA#<rank>#SCRIPT#<version>     screenplay JSON + scene_audio
pk=EPISODE#<number>     sk=IDEA#<rank>#RENDER#<version>     render status, mp4_key, execution_arn

GSI byType: gsi1pk="EPISODES", gsi1sk=zero-padded episode_number
```

Scripts and renders are **versioned** — revisions create a new version, old ones retained for history.

---

## Pipelines

### 1. Upload → Transcribe (async)

```
POST /episodes/upload-url   → presigned PUT URL
PUT  <presigned URL>        → browser uploads MP3 to S3
POST /episodes              → starts AWS Transcribe job
GET  /episodes/<id>/status  → polls transcribe job, flips to TRANSCRIBED when done
```

### 2. Ideation — 3-step decomposition (code + 2 LLM passes)

Each step does exactly one thing. This eliminates the "phrase extraction + timestamp alignment in one LLM call" fragility.

**Step 1 — Transcript cleanup** (`backend/transcript_cleanup.py`, pure code):
- Strip filler words (um, uh, you know, basically, sort of)
- Normalize punctuation
- Preserve exact timestamp mapping via `[N]` segment markers

**Step 2 — Semantic segment detection** (`backend/agents/segment_detector.py`, Opus 4.6):
- Input: cleaned transcript with `[N]` markers
- Output: `[{start_seg: N, end_seg: M, topic: "..."}]` — indices, not phrases
- Criteria: complete SETUP + ANALOGY/SCRIPTURE + LANDING structure
- Rejects mid-analogy cuts, pure Q&A, context-dependent clips
- Timestamps come from `segments_for_range()` code lookup

**Step 3 — Clip scoring** (`backend/agents/clip_scorer.py`, Sonnet 4.6):
- Weighted scoring: Emotional resonance 30%, Hook 25%, Insight 25%, Payoff 15%, Self-containment 5%
- Returns top 2-3 with title, summary, hook_line, twist_line, payoff_line
- Title rules: "Clear before clever", spiritual nouns (Krishna, Gita, Karma), question/contrast framing

### 3. Script generation (async, 2 LLM passes + FFmpeg)

**Screenwriter** (`backend/agents/screenwriter.py`, Sonnet 4.6):
- Splits the idea's window into 4-7 sequential **beats** (spoken segments)
- Each beat has 2-4 **shots** (visual clips within the beat)
- Beat purpose: hook / setup / build / twist / payoff
- Shot metadata: `shot_role`, `visual_mode`, `framing`, `camera_movement`, `transition_hint`
- Visual prompts written as literal, camera-ready descriptions (not abstract metaphors)

**Visual Director** (`backend/agents/visual_director.py`, Haiku 4.5):
- Polishes each shot's `visual`, `framing`, `camera_movement`, `transition_hint`
- Enforces shot-to-shot framing variation within beats
- Enforces beat-to-beat emotional arc progression

**Timeline normalizer** (`backend/api.py::_align_beat_timelines`):
- Forces `beat.end - beat.start == beat.source_end - beat.source_start` (reel time matches source time — no audio cut-offs)
- Enforces 180s hard cap: trims the last beat + drops trailing beats if needed
- Normalizes shot durations to tile across beat duration

**Audio slicing** (`backend/audio_slice.py`, FFmpeg Lambda layer):
- Downloads source podcast from S3 (cached per invocation)
- For each beat, slices `[source_start, source_end]` into a separate MP3
- Generates presigned URLs (2-hour TTL, regenerated on every `GET /script`)
- Bug-guard: if a beat has no timestamps, falls back to Polly TTS (should never fire)

### 4. Render (Step Functions, 3 steps)

```
Broll (Python)  →  Render (Node, Remotion)  →  Pack (Python)
```

**Broll step** (`backend/broll.py`):
- Flattens beats → shots into a global list with IDs like `b{beat_idx}_s{shot_idx}`
- For **primary shot** of each beat (`shot_idx=0`): fires Amazon Nova Reel async job ($0.48 per 6s clip)
  - Prompts prefixed with camera style per beat purpose (hook/setup/build/twist/payoff)
  - Staggered 3s apart to avoid Nova concurrency throttling
  - Exponential backoff retry on ThrottlingException
- For **secondary shots**: queries Pexels with `broll_queries` (free, instant)
- Fallback: if Nova fails, try Pexels; if Pexels fails, gradient background

**Render step** (`backend-node/remotion-invoker/index.js`):
- Uses `@remotion/lambda` SDK's `renderMediaOnLambda()` + `getRenderProgress()`
- Polls until done (up to 12 min budget), copies final MP4 into our assets bucket
- Remotion composition (`remotion/src/compositions/Reel.tsx`):
  - Each beat = `<Sequence>` containing audio + text overlay
  - Each beat's shots = nested `<Sequence>`s tiling across the beat duration
  - `<OffthreadVideo>` for b-roll (handles long clips without preload timeouts)
  - `object-fit: cover` crops Nova's 1280x720 landscape to 9:16 portrait

**Pack step** (`backend/pack.py`):
- Writes `caption.txt`, `hashtags.txt`, `metadata.json` alongside the MP4
- Updates DynamoDB RENDER item to `status=READY` + `mp4_key`

### 5. Async execution pattern

Several steps exceed the API Gateway HTTP API's 30-second integration timeout:
- Ideation (3 LLM calls on full transcript): ~30-60s
- Script generation (screenwriter + director + slice): ~2-3min

Solution: **self-invoked Lambda workers**. The HTTP handler creates a placeholder DDB item with status=`GENERATING` / `IDEATING`, fires `InvocationType=Event` on itself with a marker payload, and returns 202 immediately. The background invocation runs the heavy lifting and updates the DDB item to READY (or FAILED with a reason).

The UI polls the status endpoint and transitions when the item flips to READY.

---

## Authentication

- Cognito User Pool `vyas-video-users` (email sign-in, self-signup, email verification)
- Password policy: 8+ chars, upper + lower + digit
- ID/access token TTL: 1 hour; refresh token TTL: 30 days
- Web client uses SRP auth flow (no client secret)
- API Gateway JWT authorizer validates ID tokens on every request except `/health`
- Frontend uses `aws-amplify` v6; `lib/auth.ts` wraps sign-in/up/out; `lib/api.ts` attaches `Authorization: Bearer <id-token>` to every API call

### CORS

- API Gateway HTTP API `corsPreflight` allows all origins (`*`)
- **Explicit header list** (API Gateway's `*` wildcard for AllowHeaders is unreliable): `Authorization, Content-Type, Accept, X-Amz-Date, X-Amz-Security-Token, X-Api-Key, X-Requested-With`
- OPTIONS preflight bypasses the JWT authorizer — only `GET/POST/PUT/DELETE/PATCH` route through it

---

## Guardrails (`backend/guardrails.py`)

Every pipeline step routes through `RunContext.call()`:

```python
ctx = RunContext()
result = ctx.call(
    "step_name", fn, *args,
    model="sonnet-4.6", estimated_cost=0.04, estimated_tokens=20000,
)
```

Enforces:
- **Run budgets**: max 20 LLM calls, 8 retries, $8/run, 30 steps
- **Per-step limits**: max 2 retries for transient errors only (`ThrottlingException`, 5xx, timeouts)
- **Circuit breakers**: per-model — opens after 3 consecutive failures or 50% rolling failure rate (5-min window)
- **Loop prevention**: aborts on 4 identical outputs (stalled pipeline)
- **Structured logging**: every step logged with model, attempt, elapsed, est. cost/tokens, total budget spent

Failures surface in the UI as `SCRIPT_FAILED` or `RENDER_FAILED` with the exact reason + a retry button. `render-status` endpoint also syncs DDB status with the underlying Step Functions execution — if SFn failed silently, DDB is flipped to RENDER_FAILED automatically.

---

## Models

| Role | Model | Why |
|---|---|---|
| Topic detection (ideation step 2) | Claude Opus 4.6 (inference profile `us.anthropic.claude-opus-4-6-v1`) | Deepest reasoning over full transcript |
| Clip scoring + screenwriter (ideation step 3, script generation) | Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`) | Structured output, faster + cheaper than Opus |
| Visual director | Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) | Lightweight polish pass, very fast |
| AI video generation | Amazon Nova Reel v1:1 | Native AWS, 1280x720 landscape, async |
| Bug-guard voiceover (should never fire) | AWS Polly (Stephen voice, generative engine) | Only used when a beat has no source timestamps |

---

## Observability

- All Lambda logs → CloudWatch
- `guardrails.py::log()` emits structured lines prefixed with `[step]`, `[breaker]`, `[align]`, `[broll]`, `[nova]`
- Step Functions execution history includes full input/output per step (`scene_broll`, `render_id`, etc.)
- DDB status fields (`IDEATING`, `GENERATING`, `RENDERING`, `READY`, `*_FAILED`) are the source of truth for the UI
- Every failed state has a `failure_reason` string visible to the user

---

## Cost model

| Step | Typical cost per episode |
|---|---|
| Transcribe (20-min audio) | ~$0.50 (one-time) |
| Ideation (Opus + Sonnet) | ~$0.14 |
| Script gen + visual director + slice | ~$0.08 per idea |
| Nova Reel (10 primary shots) | ~$4.80 per reel |
| Pexels (20 secondary shots) | $0 |
| Remotion Lambda render (~2-3 min on ARM64) | ~$0.01 |
| S3 + DDB + Step Fn + CloudFront | negligible |
| **Full reel end-to-end** | **~$5-6** |

Hard budget cap: **$8 per run** (enforced by guardrails).

---

## Key design decisions

1. **Audio-first, not TTS**: the host's voice is the differentiator. FFmpeg-slices from the original podcast beat the best synthetic voice.

2. **3-step ideation decomposition**: single-pass "LLM picks phrases and code aligns them" was fragile (the "cows are." problem). Separating semantic detection (segment indices) from scoring from alignment eliminates the failure mode entirely.

3. **Multi-shot beats**: one clip per spoken segment made reels feel like slideshows. 2-4 shots per beat gives edited-sequence pacing at minimal extra cost (same audio slice, multiple b-roll).

4. **Literal, camera-ready Nova prompts**: abstract "metaphorical spiritual contemplative" prompts made Nova generate generic fog. Concrete "medium shot of a hand pouring amber liquid into a glass" aligns visuals with what the host is saying.

5. **Async workers for long tasks**: API Gateway HTTP API caps at 30s. Self-invoked Lambda workers let ideation (~60s) and script generation (~200s) run without changing the client contract.

6. **Nova primary + Pexels secondary**: 30 Nova clips per reel cost $14.40 and took 10-15 min. Using Nova only for the hero shot of each beat cuts cost and time by 3× with no perceivable quality loss.

7. **Hard 180s cap**: enforced in both prompts (soft) and code (hard trim). Reels always pass platform validation.

8. **Guardrails everywhere**: the pipeline has many LLM calls + an expensive AI video step. Without budgets, retry caps, and circuit breakers, one bad episode could burn through a significant budget.
