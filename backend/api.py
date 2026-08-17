"""HTTP API for Vyas-Video.

Flow:
  1. Client requests an upload URL   → POST /episodes/upload-url
  2. Browser PUTs MP3 to S3 directly  (presigned PUT)
  3. Client registers the episode     → POST /episodes  { episode_number, title, audio_key }
     - API starts an async AWS Transcribe job and returns status=TRANSCRIBING
  4. Client polls                     → GET /episodes/{id}/status
     - When Transcribe completes, the API stores the transcript JSON in S3
       and flips status to TRANSCRIBED
  5. Client runs ideation             → POST /episodes/{id}/ideate
     - Runs Opus 4.6 on the transcript text, persists ideas, status → READY
  6. Per-idea script / revise / render / publish (unchanged)

Data model (DynamoDB single table):
  pk=EPISODE#<n>   sk=META
    { episode_number, title, name, audio_key, transcript_key,
      transcript_json_key, transcribe_job, status, created_at }
  pk=EPISODE#<n>   sk=IDEA#<rank>
  pk=EPISODE#<n>   sk=IDEA#<rank>#SCRIPT#<version>
  pk=EPISODE#<n>   sk=IDEA#<rank>#RENDER#<version>
"""
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel

from agents.segment_detector import detect_segments
from agents.clip_scorer import score_clips
from agents.screenwriter import write_script, revise_script
from agents.visual_director import direct as direct_visuals
from agents.screenplay_judge import judge_screenplay
from transcript_cleanup import cleanup as cleanup_transcript, segments_for_range
from audio_slice import slice_scenes
from guardrails import RunContext, GuardrailsConfig, RenderBudget, GuardrailError, log as glog
from models import Idea, Screenplay

app = FastAPI(title="Vyas-Video API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

_s3 = boto3.client("s3")
_ddb = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_sfn = boto3.client("stepfunctions")
_transcribe = boto3.client("transcribe")
_lambda_client = boto3.client("lambda")

BUCKET = os.environ["ASSETS_BUCKET"]
STATE_MACHINE = os.environ["STATE_MACHINE_ARN"]
# Lazily populated from Lambda context on first invocation. Used by the async
# ideate endpoint to InvokeFunction itself. A self-Ref in CDK env would create
# a CloudFormation circular dependency, so we resolve at runtime instead.
SELF_FUNCTION_NAME = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timed_segments(timed_transcript: str) -> list[dict[str, Any]]:
    """Parse "(start-end) text" lines into [{start, end, text}, ...]."""
    import re as _re
    segments = []
    for line in timed_transcript.strip().split("\n"):
        m = _re.match(r"\((\d+\.?\d*)-(\d+\.?\d*)\)\s*(.*)", line.strip())
        if m:
            segments.append({
                "start": float(m.group(1)),
                "end": float(m.group(2)),
                "text": m.group(3),
            })
    return segments


_SENTENCE_END = frozenset(".?!:")


def _extend_window_to_sentence_end(
    idea_dict: dict[str, Any],
    segments: list[dict[str, Any]],
    max_extension: float = 12.0,
) -> dict[str, Any]:
    """If window_text ends mid-sentence, extend window_end by appending the next
    segments until we find sentence-ending punctuation or hit the max extension.

    Mutates and returns idea_dict.
    """
    wt = idea_dict.get("window_text", "").rstrip()
    we = float(idea_dict.get("window_end", 0))
    if not wt or not we:
        return idea_dict
    # Check the last meaningful word (ignoring trailing punctuation). If it's a
    # function word that normally precedes a complement (verb, conjunction,
    # article, preposition), the thought is almost certainly incomplete — even
    # if Transcribe placed a period there (it punctuates on pauses, not grammar).
    stripped = wt.rstrip(" .?!:;,")
    last_word = stripped.split()[-1].lower() if stripped else ""
    _DANGLING_WORDS = frozenset(
        "is are was were am be been being "
        "and but or nor yet so "
        "the a an "
        "to of for in on at by with from into "
        "that which who whom whose where when "
        "has have had do does did "
        "not no".split()
    )
    if last_word not in _DANGLING_WORDS:
        # Ends on a content word (noun, adjective, adverb) — likely complete.
        return idea_dict
    print(f"[extend] last word '{last_word}' is dangling — extending window despite punctuation")

    print(f"[extend] window_text ends mid-sentence: '...{wt[-60:]}'")
    extended_text = wt
    extended_end = we
    budget = max_extension

    for seg in segments:
        if seg["start"] < we - 0.5:
            continue  # segment starts before our window
        if seg["start"] > we + 1.0:
            # gap — this segment isn't adjacent
            break
        extended_text += " " + seg["text"]
        extended_end = seg["end"]
        budget -= (seg["end"] - seg["start"])
        if extended_text.rstrip()[-1] in _SENTENCE_END:
            print(f"[extend] found sentence end at {extended_end:.1f}s (+{we - float(idea_dict['window_end']):.1f}s)")
            break
        if budget <= 0:
            print(f"[extend] hit max extension budget at {extended_end:.1f}s")
            break

    idea_dict["window_text"] = extended_text.strip()
    idea_dict["window_end"] = extended_end
    idea_dict["target_length_sec"] = int(round(extended_end - float(idea_dict["window_start"])))
    return idea_dict


def _floats_to_decimal(obj: Any) -> Any:
    """Recursively convert float → Decimal for DynamoDB compatibility."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj


def _ep_pk(ep_id: int | str) -> str:
    return f"EPISODE#{ep_id}"


def _short_pk(short_id: str) -> str:
    return f"SHORT#{short_id}"


# ---------- Requests ----------


class UploadUrlRequest(BaseModel):
    episode_number: int
    filename: str  # e.g. "episode-1.mp3"


class CreateEpisodeRequest(BaseModel):
    episode_number: int
    title: str = ""
    audio_key: str  # returned from /episodes/upload-url


class ReviseScriptRequest(BaseModel):
    instruction: str


class ShortUploadUrlRequest(BaseModel):
    filename: str  # e.g. "clip.mp4"


class CreateShortRequest(BaseModel):
    title: str = ""
    video_key: str  # returned from /shorts/upload-url


class UpdateShortCaptionRequest(BaseModel):
    caption: str


class UpdateBeatsRequest(BaseModel):
    beats: list[dict[str, Any]]


# ---------- Helpers ----------


def _latest(prefix: str, pk: str) -> dict[str, Any] | None:
    resp = _ddb.query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(prefix),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def _get_meta(ep_id: int | str) -> dict[str, Any]:
    resp = _ddb.get_item(Key={"pk": _ep_pk(ep_id), "sk": "META"})
    meta = resp.get("Item")
    if not meta:
        raise HTTPException(404, "episode not found")
    return meta


def _job_name(ep_id: int | str) -> str:
    return f"vyas-video-ep-{ep_id}"


def _content_type_for(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    return {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "wav": "audio/wav",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "flac": "audio/flac",
    }.get(ext, "audio/mpeg")


def _transcribe_media_format(audio_key: str) -> str:
    ext = audio_key.lower().rsplit(".", 1)[-1]
    return {"mp3": "mp3", "m4a": "mp4", "mp4": "mp4", "wav": "wav", "ogg": "ogg", "flac": "flac"}.get(ext, "mp3")


# ---------- Routes ----------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/episodes/upload-url")
def upload_url(req: UploadUrlRequest) -> dict[str, Any]:
    """Presigned PUT for direct browser upload of the podcast audio."""
    if req.episode_number < 1:
        raise HTTPException(400, "episode_number must be >= 1")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", req.filename)[:120] or "audio.mp3"
    audio_key = f"episodes/{req.episode_number}/source/{safe_name}"
    url = _s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET,
            "Key": audio_key,
            "ContentType": _content_type_for(safe_name),
        },
        ExpiresIn=60 * 15,
    )
    return {"url": url, "audio_key": audio_key, "content_type": _content_type_for(safe_name)}


@app.post("/episodes")
def create_episode(req: CreateEpisodeRequest) -> dict[str, Any]:
    """Register an uploaded audio as a new episode and kick off AWS Transcribe.
    Ideation runs later via POST /episodes/{id}/ideate once transcription completes."""
    if req.episode_number < 1:
        raise HTTPException(400, "episode_number must be >= 1")

    ep_id = req.episode_number
    if _ddb.get_item(Key={"pk": _ep_pk(ep_id), "sk": "META"}).get("Item"):
        raise HTTPException(409, f"Episode {ep_id} already exists.")

    # Verify the browser really uploaded the file we pre-signed.
    try:
        head = _s3.head_object(Bucket=BUCKET, Key=req.audio_key)
    except _s3.exceptions.ClientError:
        raise HTTPException(400, f"audio object not found at {req.audio_key}")

    created_at = _now_iso()
    display_name = f"Episode {ep_id} — {req.title.strip()}" if req.title.strip() else f"Episode {ep_id}"

    # Start an async Transcribe job. Output is written back into our assets bucket.
    transcript_json_key = f"episodes/{ep_id}/transcript.json"
    job_name = _job_name(ep_id)
    # Clean up any dangling prior job with the same name (retrying after failure).
    try:
        _transcribe.delete_transcription_job(TranscriptionJobName=job_name)
    except _transcribe.exceptions.ClientError:
        pass
    _transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode="en-US",
        MediaFormat=_transcribe_media_format(req.audio_key),
        Media={"MediaFileUri": f"s3://{BUCKET}/{req.audio_key}"},
        OutputBucketName=BUCKET,
        OutputKey=transcript_json_key,
        Settings={"ShowSpeakerLabels": False},
    )

    _ddb.put_item(
        Item={
            "pk": _ep_pk(ep_id),
            "sk": "META",
            "episode_number": ep_id,
            "title": req.title.strip(),
            "name": display_name,
            "audio_key": req.audio_key,
            "audio_size": int(head.get("ContentLength", 0)),
            "transcript_json_key": transcript_json_key,
            "transcribe_job": job_name,
            "status": "TRANSCRIBING",
            "created_at": created_at,
            "gsi1pk": "EPISODES",
            "gsi1sk": f"{ep_id:06d}",
        }
    )

    return {
        "episode_id": str(ep_id),
        "episode_number": ep_id,
        "name": display_name,
        "status": "TRANSCRIBING",
    }


def _sync_transcribe_status(meta: dict[str, Any]) -> dict[str, Any]:
    """If the episode is TRANSCRIBING, poke Transcribe; on completion, persist the
    plaintext transcript and flip status to TRANSCRIBED."""
    if meta.get("status") != "TRANSCRIBING":
        return meta
    job_name = meta.get("transcribe_job")
    if not job_name:
        return meta
    job = _transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    s = job["TranscriptionJobStatus"]
    if s == "FAILED":
        _ddb.update_item(
            Key={"pk": meta["pk"], "sk": "META"},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "TRANSCRIBE_FAILED",
                ":r": job.get("FailureReason", "unknown"),
            },
        )
        meta["status"] = "TRANSCRIBE_FAILED"
        meta["failure_reason"] = job.get("FailureReason", "unknown")
        return meta
    if s != "COMPLETED":
        return meta

    # Read the transcript JSON, extract plain text, store a text copy for the agent.
    raw = _s3.get_object(Bucket=BUCKET, Key=meta["transcript_json_key"])["Body"].read()
    data = json.loads(raw)
    transcript_text = data["results"]["transcripts"][0]["transcript"]
    transcript_text_key = f"episodes/{meta['episode_number']}/transcript.txt"
    _s3.put_object(Bucket=BUCKET, Key=transcript_text_key, Body=transcript_text.encode())

    _ddb.update_item(
        Key={"pk": meta["pk"], "sk": "META"},
        UpdateExpression="SET #s = :s, transcript_key = :k",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "TRANSCRIBED", ":k": transcript_text_key},
    )
    meta["status"] = "TRANSCRIBED"
    meta["transcript_key"] = transcript_text_key
    return meta


@app.get("/episodes/{episode_id}/status")
def get_status(episode_id: int) -> dict[str, Any]:
    meta = _get_meta(episode_id)
    meta = _sync_transcribe_status(meta)
    return {
        "episode_id": str(episode_id),
        "status": meta.get("status"),
        "failure_reason": meta.get("failure_reason"),
    }


def _run_ideation(episode_id: int) -> None:
    """Internal worker — runs Opus 4.6 on the timed transcript, persists ideas,
    flips status to READY (or IDEATE_FAILED). Intended to run asynchronously
    via self Lambda invoke so the HTTP API Gateway 30s limit is not a factor."""
    try:
        meta = _get_meta(episode_id)
        if not meta.get("transcript_key"):
            raise RuntimeError("transcript missing")
        # 3-step ideation pipeline with production guardrails.
        ctx = RunContext()

        timed = _load_timed_transcript(episode_id)
        raw_segments = _parse_timed_segments(timed)

        # Step 1: Transcript cleanup (code, no LLM — no budget impact).
        clean_segs, clean_text, seg_idx = ctx.call(
            "ideation.cleanup", cleanup_transcript, raw_segments,
            is_llm=False, estimated_cost=0,
        )
        glog(f"[ideation] step 1: {len(raw_segments)} raw → {len(clean_segs)} clean segments")

        # Step 2: Semantic segment detection (Opus 4.6).
        topic_segments = ctx.call(
            "ideation.detect_segments", detect_segments, clean_text,
            model="opus-4.6", estimated_cost=0.10, estimated_tokens=80000,
        )
        glog(f"[ideation] step 2: {len(topic_segments)} topic candidates")

        # Build candidate list with full text + timestamps.
        # Hard cap at 180 seconds (YouTube Shorts / Instagram Reels limit).
        # If the LLM picked an end_seg that puts the window over 180s, walk
        # back to the last segment that fits.
        MAX_REEL_SEC = float(RenderBudget().max_reel_duration_sec)
        candidates = []
        for i, ts in enumerate(topic_segments):
            s_idx = int(ts["start_seg"])
            e_idx = int(ts["end_seg"])
            # Shrink the window if it exceeds the platform cap.
            while e_idx > s_idx:
                audio_start, audio_end, _ = segments_for_range(clean_segs, s_idx, e_idx)
                if (audio_end - audio_start) <= MAX_REEL_SEC:
                    break
                e_idx -= 1
            audio_start, audio_end, original_text = segments_for_range(clean_segs, s_idx, e_idx)
            dur = audio_end - audio_start
            if dur > MAX_REEL_SEC:
                glog(f"[ideation] ⚠ candidate {i} exceeds {MAX_REEL_SEC}s even after trim ({dur:.0f}s)")
            candidates.append({
                "clip_id": i,
                "topic": ts.get("topic", ""),
                "start_seg": s_idx,
                "end_seg": e_idx,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "duration_sec": round(dur),
                "text": original_text,
            })

        # Step 3: Clip scoring (Sonnet 4.6).
        scored = ctx.call(
            "ideation.score_clips", score_clips, candidates,
            model="sonnet-4.6", estimated_cost=0.04, estimated_tokens=30000,
        )
        glog(f"[ideation] step 3: {len(scored)} clips selected")
        glog(f"[ideation] run summary", **ctx.summary())

        # Build Idea objects from scored clips.
        from models import Idea as IdeaModel
        ideas_list = []
        for sc in scored:
            cid = int(sc["clip_id"])
            cand = candidates[cid] if cid < len(candidates) else candidates[0]
            ideas_list.append(IdeaModel(
                title=sc.get("title", "Untitled"),
                alt_title_1=sc.get("alt_title_1", ""),
                alt_title_2=sc.get("alt_title_2", ""),
                hook_title=sc.get("hook_title", ""),
                description=sc.get("description", ""),
                hook=sc.get("hook_line", "")[:200],
                summary=sc.get("summary", ""),
                verse_ref=sc.get("verse_ref", ""),
                target_length_sec=cand["duration_sec"],
                why_it_works=sc.get("why_it_works", ""),
                rank=int(sc.get("rank", 1)),
                window_start=cand["audio_start"],
                window_end=cand["audio_end"],
                window_text=cand["text"],
                hook_line=sc.get("hook_line", ""),
                twist_line=sc.get("twist_line", ""),
                payoff_line=sc.get("payoff_line", ""),
            ))

        for idea in ideas_list:
            item = idea.model_dump()
            item["quotes"] = json.dumps(item.get("quotes", []))
            # DDB rejects Python float; convert to Decimal recursively.
            item = _floats_to_decimal(item)
            _ddb.put_item(
                Item={
                    "pk": _ep_pk(episode_id),
                    "sk": f"IDEA#{idea.rank}",
                    **item,
                    "created_at": _now_iso(),
                }
            )
        _ddb.update_item(
            Key={"pk": _ep_pk(episode_id), "sk": "META"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "READY"},
        )
    except Exception as e:
        _ddb.update_item(
            Key={"pk": _ep_pk(episode_id), "sk": "META"},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "IDEATE_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        raise


@app.post("/episodes/{episode_id}/ideate")
def ideate(episode_id: int) -> dict[str, Any]:
    """Kick off Opus 4.6 ideation asynchronously and return 202. The worker is
    invoked via Lambda self-invoke because the API Gateway HTTP API has a hard
    30-second integration timeout, and ideation on a full podcast transcript
    frequently exceeds it.

    Idempotent: if ideas already exist, returns them immediately.
    """
    meta = _get_meta(episode_id)
    meta = _sync_transcribe_status(meta)
    if meta.get("status") == "TRANSCRIBING":
        raise HTTPException(409, "still transcribing; poll /status")
    if meta.get("status") == "TRANSCRIBE_FAILED":
        raise HTTPException(422, f"transcription failed: {meta.get('failure_reason')}")
    if not meta.get("transcript_key"):
        raise HTTPException(500, "transcript missing")

    existing = _ddb.query(
        KeyConditionExpression=Key("pk").eq(_ep_pk(episode_id))
        & Key("sk").begins_with("IDEA#"),
    ).get("Items", [])
    existing_ideas = [i for i in existing if i["sk"].count("#") == 1]
    if existing_ideas:
        existing_ideas.sort(key=lambda i: int(i["sk"].split("#")[1]))
        return {
            "episode_id": str(episode_id),
            "status": meta.get("status") or "READY",
            "ideas": [_idea_view(i) for i in existing_ideas],
        }

    # Already kicked off but not yet finished → don't fire a second worker.
    if meta.get("status") == "IDEATING":
        return {"episode_id": str(episode_id), "status": "IDEATING"}

    # Mark IDEATING and fire the async worker.
    _ddb.update_item(
        Key={"pk": _ep_pk(episode_id), "sk": "META"},
        UpdateExpression="SET #s = :s REMOVE failure_reason",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "IDEATING"},
    )
    if not SELF_FUNCTION_NAME:
        # Local dev / broken env — run sync as a fallback.
        _run_ideation(episode_id)
    else:
        _lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(
                {"internal_task": "ideate", "episode_id": int(episode_id)}
            ).encode(),
        )
    return {"episode_id": str(episode_id), "status": "IDEATING"}


@app.get("/episodes")
def list_episodes() -> dict[str, Any]:
    resp = _ddb.query(
        IndexName="byType",
        KeyConditionExpression=Key("gsi1pk").eq("EPISODES"),
        ScanIndexForward=False,
    )
    return {
        "episodes": [
            {
                "episode_id": str(int(item.get("episode_number", 0)) or item["pk"].split("#", 1)[1]),
                "name": item.get("name", ""),
                "status": item.get("status", "UNKNOWN"),
                "created_at": item.get("created_at", ""),
            }
            for item in resp.get("Items", [])
        ]
    }


def _idea_view(i: dict[str, Any]) -> dict[str, Any]:
    raw_quotes = i.get("quotes", "[]")
    if isinstance(raw_quotes, str):
        try:
            quotes = json.loads(raw_quotes)
        except json.JSONDecodeError:
            quotes = []
    else:
        quotes = raw_quotes or []
    return {
        "rank": int(i["sk"].split("#")[1]),
        "title": i.get("title", ""),
        "alt_title_1": i.get("alt_title_1", ""),
        "alt_title_2": i.get("alt_title_2", ""),
        "hook_title": i.get("hook_title", ""),
        "description": i.get("description", ""),
        "hook": i.get("hook", ""),
        "summary": i.get("summary", ""),
        "verse_ref": i.get("verse_ref", ""),
        "target_length_sec": int(i.get("target_length_sec", 30)),
        "why_it_works": i.get("why_it_works", ""),
        # Continuous window (new)
        "window_start": float(i.get("window_start", 0)),
        "window_end": float(i.get("window_end", 0)),
        "window_text": i.get("window_text", ""),
        # Narrative arc annotations
        "hook_line": i.get("hook_line", ""),
        "twist_line": i.get("twist_line", ""),
        "payoff_line": i.get("payoff_line", ""),
        # Deprecated
        "quotes": quotes,
    }


@app.get("/episodes/{episode_id}")
def get_episode(episode_id: int) -> dict[str, Any]:
    resp = _ddb.query(KeyConditionExpression=Key("pk").eq(_ep_pk(episode_id)))
    items = resp.get("Items", [])
    if not items:
        raise HTTPException(404, "episode not found")
    meta = next((i for i in items if i["sk"] == "META"), None)
    ideas_raw = [i for i in items if i["sk"].startswith("IDEA#") and i["sk"].count("#") == 1]
    ideas_raw.sort(key=lambda i: int(i["sk"].split("#")[1]))

    def _status_for(rank: int) -> tuple[bool, str | None, str | None, str | None, str | None]:
        pk = _ep_pk(episode_id)
        s_ready = _latest_ready_script(episode_id, rank)
        s_any = _latest(f"IDEA#{rank}#SCRIPT#", pk)
        r = _latest(f"IDEA#{rank}#RENDER#", pk)
        if r:
            r = _sync_render_status(r, pk)
        script_status = None
        if s_any:
            script_status = s_any.get("status") or (
                "READY" if s_any.get("screenplay") else "GENERATING"
            )
        return (
            s_ready is not None,
            s_ready["sk"].rsplit("#", 1)[-1] if s_ready else None,
            r.get("status") if r else None,
            r.get("mp4_key") if r else None,
            script_status,
        )

    ideas = []
    for i in ideas_raw:
        rank = int(i["sk"].split("#")[1])
        has_script, ver, render_status, mp4_key, script_status = _status_for(rank)
        ideas.append(
            {
                **_idea_view(i),
                "has_script": has_script,
                "script_version": ver,
                "script_status": script_status,
                "render_status": render_status,
                "render_mp4_key": mp4_key,
            }
        )

    return {
        "episode_id": str(episode_id),
        "name": (meta or {}).get("name", ""),
        "status": (meta or {}).get("status", "UNKNOWN"),
        "created_at": (meta or {}).get("created_at", ""),
        "ideas": ideas,
    }


# ---------- Per-idea script / revise / render (unchanged shape) ----------


def _load_idea(ep_id: int, rank: int) -> Idea:
    item = _ddb.get_item(Key={"pk": _ep_pk(ep_id), "sk": f"IDEA#{rank}"}).get("Item")
    if not item:
        raise HTTPException(404, "idea not found")
    raw_quotes = item.get("quotes", "[]")
    if isinstance(raw_quotes, str):
        try:
            quotes = json.loads(raw_quotes)
        except json.JSONDecodeError:
            quotes = []
    else:
        quotes = raw_quotes or []
    return Idea(
        title=item["title"],
        alt_title_1=item.get("alt_title_1", ""),
        alt_title_2=item.get("alt_title_2", ""),
        hook_title=item.get("hook_title", ""),
        description=item.get("description", ""),
        hook=item["hook"],
        summary=item["summary"],
        verse_ref=item["verse_ref"],
        target_length_sec=int(item["target_length_sec"]),
        why_it_works=item["why_it_works"],
        rank=int(item["sk"].split("#")[1]),
        window_start=float(item.get("window_start", 0)),
        window_end=float(item.get("window_end", 0)),
        window_text=item.get("window_text", ""),
        hook_line=item.get("hook_line", ""),
        twist_line=item.get("twist_line", ""),
        payoff_line=item.get("payoff_line", ""),
        quotes=quotes,
    )


def _load_transcript(ep_id: int) -> str:
    meta = _get_meta(ep_id)
    if not meta.get("transcript_key"):
        raise HTTPException(400, "transcript not ready")
    return _s3.get_object(Bucket=BUCKET, Key=meta["transcript_key"])["Body"].read().decode()


def _load_timed_transcript(ep_id: int) -> str:
    """Return the transcript as timed segments for the screenwriter to quote from.

    Format: one segment per line, `(start-end) text`. Times in seconds.
    """
    meta = _get_meta(ep_id)
    if not meta.get("transcript_json_key"):
        raise HTTPException(400, "timed transcript not ready")
    raw = _s3.get_object(Bucket=BUCKET, Key=meta["transcript_json_key"])["Body"].read()
    data = json.loads(raw)

    segments = data["results"].get("audio_segments")
    if not segments:
        # Older Transcribe responses: build sentence-ish segments from items.
        items = data["results"].get("items", [])
        chunks, current, c_start = [], [], None
        for it in items:
            if it["type"] == "pronunciation":
                if c_start is None:
                    c_start = float(it["start_time"])
                current.append(it["alternatives"][0]["content"])
                c_end = float(it["end_time"])
            else:  # punctuation
                if current:
                    chunks.append({"start_time": c_start, "end_time": c_end, "text": " ".join(current) + it["alternatives"][0]["content"]})
                    current, c_start = [], None
        if current:
            chunks.append({"start_time": c_start, "end_time": c_end, "text": " ".join(current)})
        segments = chunks

    lines = [
        f"({float(s['start_time']):.2f}-{float(s['end_time']):.2f}) {s['transcript'] if 'transcript' in s else s['text']}"
        for s in segments
    ]
    return "\n".join(lines)


def _script_response(item: dict[str, Any]) -> dict[str, Any]:
    """Combine stored screenplay JSON + stored scene_audio into the /script response.
    For items still GENERATING, returns {status: ...} only."""
    status = item.get("status") or ("READY" if item.get("screenplay") else "GENERATING")
    if status != "READY" or not item.get("screenplay"):
        return {
            "status": status,
            "version": item["sk"].rsplit("#", 1)[-1],
            "failure_reason": item.get("failure_reason"),
        }
    sp = json.loads(item["screenplay"])
    raw = item.get("scene_audio")
    if isinstance(raw, str):
        scene_audio = json.loads(raw)
    else:
        scene_audio = raw or []
    # Regenerate presigned URLs on every read — stored URLs expire after 2h.
    for entry in scene_audio:
        key = entry.get("audio_key")
        if key:
            entry["audio_url"] = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET, "Key": key},
                ExpiresIn=60 * 60 * 2,
            )
    return {
        **sp,
        "scene_audio": scene_audio,
        "status": "READY",
        "version": item["sk"].rsplit("#", 1)[-1],
    }


def _latest_ready_script(ep_id: int, rank: int) -> dict[str, Any] | None:
    """Walk SCRIPT# items from newest to oldest; return the first that is READY
    (has a `screenplay` field). Skips in-flight GENERATING items."""
    resp = _ddb.query(
        KeyConditionExpression=Key("pk").eq(_ep_pk(ep_id))
        & Key("sk").begins_with(f"IDEA#{rank}#SCRIPT#"),
        ScanIndexForward=False,
        Limit=10,
    )
    for item in resp.get("Items", []):
        if item.get("screenplay"):
            return item
    return None


@app.get("/episodes/{episode_id}/ideas/{rank}/script")
def get_latest_script(episode_id: int, rank: int) -> dict[str, Any]:
    """Return the latest script for an idea. If the newest SCRIPT# item is
    still GENERATING, we return its status so the UI can poll. If no script
    exists at all, 404."""
    latest = _latest(f"IDEA#{rank}#SCRIPT#", _ep_pk(episode_id))
    if not latest:
        raise HTTPException(404, "no script yet")
    return _script_response(latest)


def _with_visual_director(screenplay: Screenplay) -> Screenplay:
    """Run the visual-director pass. Swallow failures — a bad director call
    never blocks script generation; we fall back to screenwriter queries."""
    try:
        return direct_visuals(screenplay)
    except Exception as e:
        print(f"[visual_director] failed, keeping screenwriter queries: {e!r}")
        return screenplay


MAX_REEL_DURATION_SEC = float(RenderBudget().max_reel_duration_sec)


def _align_beat_timelines(screenplay: Screenplay) -> Screenplay:
    """Force reel timeline to equal source spans, beat by beat.

    Also enforces the 180-second platform cap by truncating trailing beats
    (and/or shrinking the last included beat) so the reel fits within
    YouTube Shorts / Instagram Reels duration limits.
    """
    t = 0.0
    kept_beats = []
    for i, beat in enumerate(screenplay.beats):
        if beat.source_start is None or beat.source_end is None:
            dur = max(0.5, float(beat.end) - float(beat.start))
        else:
            dur = max(0.5, float(beat.source_end) - float(beat.source_start))

        remaining = MAX_REEL_DURATION_SEC - t
        if remaining <= 0.5:
            print(f"[align] ⚠ dropping beat {i+1} — reel already at {t:.0f}s (cap {MAX_REEL_DURATION_SEC:.0f}s)")
            continue
        if dur > remaining:
            # Trim this beat to fit within the cap.
            print(f"[align] trimming beat {i+1} from {dur:.1f}s to {remaining:.1f}s to stay under 180s")
            dur = remaining
            if beat.source_start is not None:
                beat.source_end = float(beat.source_start) + dur

        beat.start = round(t, 2)
        beat.end = round(t + dur, 2)
        if beat.shots:
            # ── Minimum shot duration enforcement ──────────────────────────
            # Nova Reel clips are 6s but we display them for shot_duration_sec.
            # Anything under 3s cuts too fast to read. If the beat is short
            # relative to its shot count, trim excess shots (keep first N)
            # so every remaining shot gets at least MIN_SHOT_SEC on screen.
            MIN_SHOT_SEC = 3.0
            max_shots = max(1, int(dur / MIN_SHOT_SEC))
            if len(beat.shots) > max_shots:
                print(
                    f"[align] beat {i+1} ({dur:.1f}s): trimming {len(beat.shots)} shots "
                    f"→ {max_shots} (min {MIN_SHOT_SEC}s/shot)"
                )
                beat.shots = beat.shots[:max_shots]

            # Distribute beat duration evenly across remaining shots.
            n = len(beat.shots)
            per_shot = round(dur / n, 2)
            for j, s in enumerate(beat.shots):
                # Last shot absorbs rounding remainder so shots sum exactly to dur.
                s.shot_duration_sec = per_shot if j < n - 1 else round(dur - per_shot * (n - 1), 2)
        t += dur
        kept_beats.append(beat)

    screenplay.beats = kept_beats
    screenplay.duration_sec = int(round(t))
    if t > MAX_REEL_DURATION_SEC:
        print(f"[align] ⚠ final duration {t:.1f}s exceeds {MAX_REEL_DURATION_SEC:.0f}s cap")

    # Continuity check.
    for i in range(1, len(screenplay.beats)):
        prev = screenplay.beats[i - 1]
        curr = screenplay.beats[i]
        if prev.source_end is not None and curr.source_start is not None:
            gap = abs(float(curr.source_start) - float(prev.source_end))
            if gap > 1.0:
                print(f"[align] ⚠ gap {gap:.1f}s between beat {i} and {i+1}")
    return screenplay


def _run_script_task(
    *,
    kind: str,
    episode_id: int,
    rank: int,
    version: str,
    instruction: str = "",
) -> None:
    """Background worker: screenwriter → visual director → audio slice.
    Updates SCRIPT#<version> with the finished artifacts on success, or
    SCRIPT_FAILED + failure_reason on error.

    Runs async (not inside an HTTP request) so it's not bound by the API
    Gateway HTTP 30-second integration timeout."""
    try:
        ctx = RunContext()
        meta = _get_meta(episode_id)
        if not meta.get("audio_key"):
            raise RuntimeError("episode has no source audio")

        if kind == "generate":
            idea = _load_idea(episode_id, rank)
            timed = _load_timed_transcript(episode_id)
            screenplay = ctx.call(
                "script.write", write_script, idea.model_dump(), timed,
                model="sonnet-4.6", estimated_cost=0.04, estimated_tokens=20000,
            )
        elif kind == "revise":
            current = _latest_ready_script(episode_id, rank)
            if not current:
                raise RuntimeError("no ready script to revise from")
            base = Screenplay(**json.loads(current["screenplay"]))
            screenplay = ctx.call(
                "script.revise", revise_script, base, instruction,
                model="sonnet-4.6", estimated_cost=0.03, estimated_tokens=15000,
            )
        else:
            raise RuntimeError(f"unknown script task kind: {kind!r}")

        screenplay = ctx.call(
            "script.visual_director", _with_visual_director, screenplay,
            model="haiku-4.5", estimated_cost=0.005, estimated_tokens=5000,
        )
        screenplay = _align_beat_timelines(screenplay)
        script_dict = screenplay.model_dump()
        scene_audio = ctx.call(
            "script.audio_slice", slice_scenes,
            episode_id=episode_id, idea_rank=rank, version=version,
            script=script_dict, source_audio_key=meta["audio_key"],
            is_llm=False, estimated_cost=0,
        )
        glog("[script] run summary", **ctx.summary())

        _ddb.update_item(
            Key={"pk": _ep_pk(episode_id), "sk": f"IDEA#{rank}#SCRIPT#{version}"},
            UpdateExpression=(
                "SET screenplay = :sp, scene_audio = :sa, #st = :r REMOVE failure_reason"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":sp": screenplay.model_dump_json(),
                ":sa": json.dumps(scene_audio),
                ":r": "READY",
            },
        )
    except Exception as e:
        print(f"[script-task] failed: {e!r}")
        _ddb.update_item(
            Key={"pk": _ep_pk(episode_id), "sk": f"IDEA#{rank}#SCRIPT#{version}"},
            UpdateExpression="SET #st = :f, failure_reason = :r",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":f": "SCRIPT_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        raise


def _kickoff_script_task(
    kind: str, episode_id: int, rank: int, instruction: str = ""
) -> dict[str, Any]:
    """Create a SCRIPT#<version> placeholder in GENERATING state and fire an
    async self-invoke worker. Returns the version so the UI can poll."""
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    placeholder: dict[str, Any] = {
        "pk": _ep_pk(episode_id),
        "sk": f"IDEA#{rank}#SCRIPT#{version}",
        "status": "GENERATING",
        "kind": kind,
        "created_at": _now_iso(),
    }
    if kind == "revise":
        placeholder["instruction"] = instruction
    _ddb.put_item(Item=placeholder)

    payload = {
        "internal_task": "script",
        "kind": kind,
        "episode_id": int(episode_id),
        "rank": int(rank),
        "version": version,
        "instruction": instruction,
    }
    if not SELF_FUNCTION_NAME:
        _run_script_task(
            kind=kind, episode_id=int(episode_id), rank=int(rank),
            version=version, instruction=instruction,
        )
    else:
        _lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    return {
        "episode_id": str(episode_id),
        "rank": rank,
        "version": version,
        "status": "GENERATING",
    }


@app.post("/episodes/{episode_id}/ideas/{rank}/script")
def generate_script(episode_id: int, rank: int) -> dict[str, Any]:
    return _kickoff_script_task("generate", episode_id, rank)


@app.post("/episodes/{episode_id}/ideas/{rank}/revise")
def revise(episode_id: int, rank: int, req: ReviseScriptRequest) -> dict[str, Any]:
    return _kickoff_script_task("revise", episode_id, rank, req.instruction)


@app.get("/episodes/{episode_id}/ideas/{rank}/script-status")
def script_status(episode_id: int, rank: int) -> dict[str, Any]:
    """Status of the latest SCRIPT item. UI polls this while a script is
    being generated in the background."""
    latest = _latest(f"IDEA#{rank}#SCRIPT#", _ep_pk(episode_id))
    if not latest:
        return {"status": "NONE"}
    return {
        "status": latest.get("status")
        or ("READY" if latest.get("screenplay") else "GENERATING"),
        "version": latest["sk"].rsplit("#", 1)[-1],
        "kind": latest.get("kind"),
        "failure_reason": latest.get("failure_reason"),
    }


@app.post("/episodes/{episode_id}/ideas/{rank}/render")
def render(episode_id: int, rank: int) -> dict[str, str]:
    # Only render from a READY script; skip in-flight GENERATING items.
    current = _latest_ready_script(episode_id, rank)
    if not current:
        raise HTTPException(400, "no ready script to render")

    # scene_audio was produced when /script ran — pass it through to the
    # render pipeline. No AudioSlice step involved any more.
    raw_audio = current.get("scene_audio")
    if isinstance(raw_audio, str):
        scene_audio = json.loads(raw_audio)
    else:
        scene_audio = raw_audio or []
    if not scene_audio:
        raise HTTPException(400, "script has no scene_audio; regenerate the script")

    # Regenerate presigned URLs for every scene — the URLs stored in the
    # script have a 2-hour TTL and may have expired (especially on retry
    # renders). Remotion fails with "Error while downloading" otherwise.
    for entry in scene_audio:
        key = entry.get("audio_key")
        if key:
            entry["audio_url"] = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET, "Key": key},
                ExpiresIn=60 * 60 * 4,  # 4 hours — long enough for the render
            )

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    script_key = f"episodes/{episode_id}/idea-{rank}/script-{version}.json"
    body = current["screenplay"] if isinstance(current["screenplay"], str) else json.dumps(current["screenplay"])
    _s3.put_object(Bucket=BUCKET, Key=script_key, Body=body.encode())

    execution = _sfn.start_execution(
        stateMachineArn=STATE_MACHINE,
        input=json.dumps(
            {
                "episode_id": episode_id,
                "idea_rank": rank,
                "version": version,
                "script_s3_key": script_key,
                "scene_audio": scene_audio,
                "project_id": f"{episode_id}/idea-{rank}",
            }
        ),
    )

    _ddb.put_item(
        Item={
            "pk": _ep_pk(episode_id),
            "sk": f"IDEA#{rank}#RENDER#{version}",
            "status": "RENDERING",
            "execution_arn": execution["executionArn"],
            "created_at": _now_iso(),
        }
    )
    return {"execution_arn": execution["executionArn"], "status": "RENDERING", "version": version}


def _sync_render_status(r: dict[str, Any], pk: str) -> dict[str, Any]:
    """If the DDB render item is stuck at RENDERING, check the underlying Step
    Function execution. If it's FAILED/TIMED_OUT/ABORTED, flip DDB to
    RENDER_FAILED with a reason. Prevents forever-RENDERING UI state."""
    if r.get("status") != "RENDERING":
        return r
    arn = r.get("execution_arn")
    if not arn:
        return r
    try:
        desc = _sfn.describe_execution(executionArn=arn)
    except Exception as e:
        print(f"[render-status] describe_execution failed: {e}")
        return r
    sfn_status = desc.get("status")
    if sfn_status == "SUCCEEDED":
        # Pack Lambda should've flipped status to READY — but if it crashed
        # after SFn succeeded, we'd see this. Treat as failure.
        if not r.get("mp4_key"):
            _ddb.update_item(
                Key={"pk": pk, "sk": r["sk"]},
                UpdateExpression="SET #s = :s, failure_reason = :rr",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "RENDER_FAILED",
                    ":rr": "Pipeline completed but no MP4 produced",
                },
            )
            r["status"] = "RENDER_FAILED"
            r["failure_reason"] = "Pipeline completed but no MP4 produced"
    elif sfn_status in ("FAILED", "TIMED_OUT", "ABORTED"):
        cause = desc.get("cause", "")[:400] or desc.get("error", sfn_status)
        _ddb.update_item(
            Key={"pk": pk, "sk": r["sk"]},
            UpdateExpression="SET #s = :s, failure_reason = :rr",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "RENDER_FAILED",
                ":rr": cause,
            },
        )
        r["status"] = "RENDER_FAILED"
        r["failure_reason"] = cause
    return r


@app.get("/episodes/{episode_id}/ideas/{rank}/render-status")
def render_status(episode_id: int, rank: int) -> dict[str, Any]:
    pk = _ep_pk(episode_id)
    r = _latest(f"IDEA#{rank}#RENDER#", pk)
    if not r:
        return {"status": "NONE"}
    r = _sync_render_status(r, pk)
    return {
        "status": r.get("status"),
        "mp4_key": r.get("mp4_key"),
        "execution_arn": r.get("execution_arn"),
        "version": r["sk"].rsplit("#", 1)[-1],
        "failure_reason": r.get("failure_reason"),
    }


@app.get("/assets/url")
def asset_url(key: str) -> dict[str, str]:
    if not key.startswith("episodes/") and not key.startswith("shorts/"):
        raise HTTPException(400, "key must be under episodes/ or shorts/")
    url = _s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600
    )
    return {"url": url}


# ============================================================
# SHORTS — short audio-to-video pipeline
# ============================================================
# User uploads a short video clip (30-180s) with baked-in audio.
# We keep the audio verbatim, generate fresh visuals, and render
# through the same Remotion + outro pipeline as full episodes.
#
# Pipeline: upload video → extract audio (FFmpeg) → Transcribe →
#           Screenwriter → Visual Director → audio_slice →
#           Step Functions (Broll → Render → Pack)
# ============================================================

_VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "avi", "mkv"}


def _short_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    return {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "m4v": "video/x-m4v",
        "webm": "video/webm",
        "avi": "video/x-msvideo",
        "mkv": "video/x-matroska",
    }.get(ext, "video/mp4")


def _short_transcribe_media_format(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    # Transcribe supports mp4 and mov directly — no audio pre-extraction needed.
    return ext if ext in {"mp4", "mov", "m4v", "webm", "ogg", "flac"} else "mp4"


def _load_short_meta(short_id: str) -> dict[str, Any]:
    item = _ddb.get_item(Key={"pk": _short_pk(short_id), "sk": "META"}).get("Item")
    if not item:
        raise HTTPException(404, f"short {short_id!r} not found")
    return item


def _sync_short_transcribe_status(meta: dict[str, Any]) -> dict[str, Any]:
    """Same pattern as _sync_transcribe_status but for shorts."""
    if meta.get("status") != "TRANSCRIBING":
        return meta
    job_name = meta.get("transcribe_job")
    if not job_name:
        return meta
    job = _transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    s = job["TranscriptionJobStatus"]
    if s == "FAILED":
        _ddb.update_item(
            Key={"pk": meta["pk"], "sk": "META"},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "TRANSCRIBE_FAILED",
                ":r": job.get("FailureReason", "unknown"),
            },
        )
        meta["status"] = "TRANSCRIBE_FAILED"
        meta["failure_reason"] = job.get("FailureReason", "unknown")
        return meta
    if s != "COMPLETED":
        return meta

    short_id = meta["short_id"]
    raw = _s3.get_object(Bucket=BUCKET, Key=meta["transcript_json_key"])["Body"].read()
    data = json.loads(raw)
    items = data["results"]["items"]

    # Build timed transcript: "(start-end) text\n" per word group
    transcript_text = data["results"]["transcripts"][0]["transcript"]
    timed_lines = []
    for item in items:
        if item["type"] != "pronunciation":
            continue
        st = item.get("start_time", "0")
        et = item.get("end_time", "0")
        word = item["alternatives"][0]["content"]
        timed_lines.append(f"({st}-{et}) {word}")
    timed_text = "\n".join(timed_lines)

    transcript_txt_key = f"shorts/{short_id}/transcript.txt"
    transcript_timed_key = f"shorts/{short_id}/transcript_timed.txt"
    _s3.put_object(Bucket=BUCKET, Key=transcript_txt_key, Body=transcript_text.encode())
    _s3.put_object(Bucket=BUCKET, Key=transcript_timed_key, Body=timed_text.encode())

    # Estimate duration from last word's end time
    last_end = 0.0
    for item in reversed(items):
        if item["type"] == "pronunciation" and item.get("end_time"):
            last_end = float(item["end_time"])
            break

    _ddb.update_item(
        Key={"pk": meta["pk"], "sk": "META"},
        UpdateExpression="SET #s = :s, transcript_key = :k, transcript_timed_key = :t, duration_sec = :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "TRANSCRIBED",
            ":k": transcript_txt_key,
            ":t": transcript_timed_key,
            ":d": Decimal(str(round(last_end, 2))),
        },
    )
    meta["status"] = "TRANSCRIBED"
    meta["transcript_key"] = transcript_txt_key
    meta["transcript_timed_key"] = transcript_timed_key
    meta["duration_sec"] = last_end
    return meta


def _run_short_generate(short_id: str, version: str) -> None:
    """Background worker: Screenwriter → Visual Director → audio_slice → SFn.
    Updates RENDER#{version} item with status=RENDERING on success, or
    RENDER_FAILED on error."""
    pk = _short_pk(short_id)
    render_sk = f"RENDER#{version}"
    try:
        ctx = RunContext()
        meta = _load_short_meta(short_id)

        # Load plain + timed transcripts.
        plain_text = _s3.get_object(Bucket=BUCKET, Key=meta["transcript_key"])["Body"].read().decode()
        timed_text = _s3.get_object(Bucket=BUCKET, Key=meta["transcript_timed_key"])["Body"].read().decode()

        duration_sec = float(meta.get("duration_sec", 60))
        title = meta.get("title") or "Short Reel"

        # Build a synthetic idea — the whole clip is the content.
        idea = {
            "title": title,
            "rank": 1,
            "hook": title,
            "summary": "",
            "target_length_sec": int(round(duration_sec)),
            "window_start": 0.0,
            "window_end": duration_sec,
            "window_text": plain_text,
            "hook_line": "",
            "twist_line": "",
            "payoff_line": "",
        }

        # Screenwriter: plain text idea + timed transcript (for source timestamps).
        screenplay = ctx.call(
            "short.write", write_script, idea, timed_text,
            model="sonnet-4.6", estimated_cost=0.04, estimated_tokens=20000,
        )

        # Visual Director: polish shot prompts.
        screenplay = ctx.call(
            "short.visual_director", _with_visual_director, screenplay,
            model="haiku-4.5", estimated_cost=0.005, estimated_tokens=5000,
        )
        screenplay = _align_beat_timelines(screenplay)
        script_dict = screenplay.model_dump()

        # Judge: evaluate the post-Visual-Director screenplay quality.
        # Non-fatal — a judge failure must never block the render.
        evaluation: dict | None = None
        try:
            evaluation = ctx.call(
                "short.judge", judge_screenplay, screenplay,
                model="haiku-4.5", estimated_cost=0.005, estimated_tokens=5000,
                is_llm=True,
            )
            print(f"[short.judge] score={evaluation.get('overall_score')} verdict={evaluation.get('verdict')}")
        except Exception as _je:
            print(f"[short.judge] non-fatal, skipping: {_je!r}")

        # Store screenplay JSON in S3 for broll Lambda to read.
        script_s3_key = f"shorts/{short_id}/render-{version}/screenplay.json"
        _s3.put_object(
            Bucket=BUCKET,
            Key=script_s3_key,
            Body=json.dumps(script_dict).encode(),
            ContentType="application/json",
        )

        # Audio slice: use the extracted audio from the video.
        audio_prefix = f"shorts/{short_id}/render-{version}/audio"
        scene_audio = ctx.call(
            "short.audio_slice", slice_scenes,
            episode_id=short_id, idea_rank=0, version=version,
            script=script_dict, source_audio_key=meta["audio_key"],
            audio_prefix=audio_prefix,
            is_llm=False, estimated_cost=0,
        )
        glog("[short.generate] run summary", **ctx.summary())

        # Kick off Step Functions render (Broll → Render → Pack).
        # Pass ddb_pk/ddb_sk so pack.py updates SHORT#{id} / RENDER#{v}.
        output_key = f"shorts/{short_id}/render-{version}/final.mp4"
        execution = _sfn.start_execution(
            stateMachineArn=STATE_MACHINE,
            name=f"short-{short_id[:8]}-{version}",
            input=json.dumps(
                _floats_to_decimal({
                    "episode_id": short_id,
                    "idea_rank": 0,
                    "version": version,
                    "script_s3_key": script_s3_key,
                    "source_audio_key": meta["audio_key"],
                    "scene_audio": scene_audio,
                    "output_key": output_key,
                    # shorts-specific overrides for pack.py
                    "ddb_pk": pk,
                    "ddb_sk": render_sk,
                }),
                default=str,
            ),
        )

        _ddb.update_item(
            Key={"pk": pk, "sk": render_sk},
            UpdateExpression="SET #s = :s, execution_arn = :e, screenplay = :sp, evaluation = :ev",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "RENDERING",
                ":e": execution["executionArn"],
                ":sp": screenplay.model_dump_json(),
                ":ev": json.dumps(evaluation) if evaluation else json.dumps({}),
            },
        )
        _ddb.update_item(
            Key={"pk": pk, "sk": "META"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "RENDERING"},
        )
    except Exception as e:
        print(f"[short.generate] failed: {e!r}")
        _ddb.update_item(
            Key={"pk": pk, "sk": render_sk},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "GENERATE_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        _ddb.update_item(
            Key={"pk": pk, "sk": "META"},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "GENERATE_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        raise


def _run_short_rerender(short_id: str, version: str) -> None:
    """Background worker: skip Screenwriter + Visual Director; re-render with the
    existing (possibly user-edited) screenplay stored in RENDER#{version}."""
    pk = _short_pk(short_id)
    render_sk = f"RENDER#{version}"
    try:
        meta = _load_short_meta(short_id)

        # Load screenplay the rerender endpoint already wrote into the new RENDER# item.
        render_item = _ddb.get_item(Key={"pk": pk, "sk": render_sk}).get("Item", {})
        sp_raw = render_item.get("screenplay")
        if not sp_raw:
            raise ValueError("screenplay missing from render item")
        sp_dict = json.loads(sp_raw) if isinstance(sp_raw, str) else sp_raw

        # Re-apply shot trimming (MIN_SHOT_SEC enforcement).
        # Old screenplays may have many short shots that exceed Nova's rate
        # limit. _align_beat_timelines trims shots to ≥3s each, reducing
        # total shot count to fit within the broll Lambda's 15-min budget.
        try:
            screenplay = Screenplay(**sp_dict)
            screenplay = _align_beat_timelines(screenplay)
            sp_dict = screenplay.model_dump()
            sp_json_str = screenplay.model_dump_json()
            print(
                f"[short.rerender] re-aligned: {len(screenplay.beats)} beats, "
                f"{sum(len(b.shots) for b in screenplay.beats)} shots, "
                f"{screenplay.duration_sec}s"
            )
        except Exception as e:
            print(f"[short.rerender] re-align failed (using original): {e!r}")
            sp_json_str = sp_raw if isinstance(sp_raw, str) else json.dumps(sp_raw)

        # Store screenplay in S3 (broll Lambda reads it from there).
        script_s3_key = f"shorts/{short_id}/render-{version}/screenplay.json"
        _s3.put_object(
            Bucket=BUCKET, Key=script_s3_key,
            Body=sp_json_str.encode(), ContentType="application/json",
        )

        # Re-slice audio (same source track, new version prefix).
        audio_prefix = f"shorts/{short_id}/render-{version}/audio"
        ctx = RunContext()
        scene_audio = ctx.call(
            "short.audio_slice", slice_scenes,
            episode_id=short_id, idea_rank=0, version=version,
            script=sp_dict, source_audio_key=meta["audio_key"],
            audio_prefix=audio_prefix,
            is_llm=False, estimated_cost=0,
        )
        glog("[short.rerender] run summary", **ctx.summary())

        output_key = f"shorts/{short_id}/render-{version}/final.mp4"
        execution = _sfn.start_execution(
            stateMachineArn=STATE_MACHINE,
            name=f"short-{short_id[:8]}-{version}",
            input=json.dumps(
                _floats_to_decimal({
                    "episode_id": short_id,
                    "idea_rank": 0,
                    "version": version,
                    "script_s3_key": script_s3_key,
                    "source_audio_key": meta["audio_key"],
                    "scene_audio": scene_audio,
                    "output_key": output_key,
                    "ddb_pk": pk,
                    "ddb_sk": render_sk,
                }),
                default=str,
            ),
        )

        _ddb.update_item(
            Key={"pk": pk, "sk": render_sk},
            UpdateExpression="SET #s = :s, execution_arn = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "RENDERING",
                ":e": execution["executionArn"],
            },
        )
        _ddb.update_item(
            Key={"pk": pk, "sk": "META"},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "RENDERING"},
        )
    except Exception as e:
        print(f"[short.rerender] failed: {e!r}")
        _ddb.update_item(
            Key={"pk": pk, "sk": render_sk},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "RENDER_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        _ddb.update_item(
            Key={"pk": pk, "sk": "META"},
            UpdateExpression="SET #s = :s, failure_reason = :r",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "RENDER_FAILED",
                ":r": f"{type(e).__name__}: {e}"[:500],
            },
        )
        raise


@app.post("/shorts/upload-url")
def short_upload_url(req: ShortUploadUrlRequest) -> dict[str, Any]:
    """Presigned PUT for direct browser upload of the short video."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", req.filename)[:120] or "clip.mp4"
    short_id = str(uuid.uuid4())
    video_key = f"shorts/{short_id}/source/{safe_name}"
    content_type = _short_content_type(safe_name)
    url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": video_key, "ContentType": content_type},
        ExpiresIn=60 * 30,
    )
    return {"url": url, "video_key": video_key, "short_id": short_id, "content_type": content_type}


@app.post("/shorts")
def create_short(req: CreateShortRequest) -> dict[str, Any]:
    """Register an uploaded video as a new short and kick off Transcribe.
    Transcribe accepts video (MP4/MOV) directly — no pre-extraction needed."""
    video_key = req.video_key
    # short_id is embedded in the key: shorts/{short_id}/source/{filename}
    try:
        short_id = video_key.split("/")[1]
    except IndexError:
        raise HTTPException(400, "invalid video_key format")

    # Verify upload exists.
    try:
        head = _s3.head_object(Bucket=BUCKET, Key=video_key)
    except Exception:
        raise HTTPException(400, f"video not found at {video_key}")

    # Check for duplicate.
    if _ddb.get_item(Key={"pk": _short_pk(short_id), "sk": "META"}).get("Item"):
        raise HTTPException(409, f"short {short_id} already registered")

    # Derive the ext from the key for Transcribe media format.
    filename = video_key.rsplit("/", 1)[-1]
    media_format = _short_transcribe_media_format(filename)

    transcript_json_key = f"shorts/{short_id}/transcript.json"
    job_name = f"vyas-video-short-{short_id[:8]}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    # Extract audio for slicing later: ffmpeg -i source -vn -acodec libmp3lame.
    # We do this inline (short clips ≤180s → ≤5s FFmpeg time on Lambda).
    import subprocess
    import tempfile as _tempfile
    audio_key = f"shorts/{short_id}/audio.mp3"
    try:
        with _tempfile.TemporaryDirectory(prefix="short-audio-") as work:
            src_local = os.path.join(work, "source")
            audio_local = os.path.join(work, "audio.mp3")
            _s3.download_file(BUCKET, video_key, src_local)
            subprocess.run(
                ["/opt/bin/ffmpeg", "-y", "-i", src_local,
                 "-vn", "-acodec", "libmp3lame", "-b:a", "128k", audio_local],
                check=True, capture_output=True,
            )
            with open(audio_local, "rb") as af:
                _s3.put_object(Bucket=BUCKET, Key=audio_key, Body=af.read(),
                               ContentType="audio/mpeg")
    except Exception as e:
        raise HTTPException(500, f"audio extraction failed: {e}")

    # Start Transcribe on the video (supports MP4/MOV directly).
    _transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode="en-US",
        MediaFormat=media_format,
        Media={"MediaFileUri": f"s3://{BUCKET}/{video_key}"},
        OutputBucketName=BUCKET,
        OutputKey=transcript_json_key,
        Settings={"ShowSpeakerLabels": False},
    )

    created_at = _now_iso()
    _ddb.put_item(
        Item={
            "pk": _short_pk(short_id),
            "sk": "META",
            "short_id": short_id,
            "title": req.title.strip() or filename,
            "video_key": video_key,
            "audio_key": audio_key,
            "transcript_json_key": transcript_json_key,
            "transcribe_job": job_name,
            "status": "TRANSCRIBING",
            "created_at": created_at,
            "gsi1pk": "SHORTS",
            "gsi1sk": created_at,
        }
    )
    return {"short_id": short_id, "status": "TRANSCRIBING"}


@app.get("/shorts/{short_id}/status")
def short_status(short_id: str) -> dict[str, Any]:
    meta = _load_short_meta(short_id)
    meta = _sync_short_transcribe_status(meta)
    pk = _short_pk(short_id)
    caption: str | None = None
    hashtags: list[str] = []
    evaluation: dict | None = None

    # Sync render status when still in flight.
    if meta.get("status") in ("RENDERING",):
        render = _latest("RENDER#", pk)
        if render:
            render = _sync_render_status(render, pk)
            if render.get("status") == "READY":
                meta["status"] = "READY"
                meta["mp4_key"] = render.get("mp4_key")
            elif render.get("status") == "RENDER_FAILED":
                meta["status"] = "RENDER_FAILED"
                meta["failure_reason"] = render.get("failure_reason")

    # Pull caption, hashtags, and evaluation from the RENDER# item when done.
    if meta.get("status") == "READY":
        render_item = _latest("RENDER#", pk)
        if render_item:
            try:
                sp_raw = render_item.get("screenplay")
                sp = json.loads(sp_raw) if isinstance(sp_raw, str) else (sp_raw or {})
                caption = sp.get("caption") or None
                hashtags = sp.get("hashtags") or []
            except Exception:
                pass
            try:
                ev_raw = render_item.get("evaluation")
                if ev_raw:
                    ev = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                    evaluation = ev if ev else None
            except Exception:
                pass

    return {
        "short_id": short_id,
        "title": meta.get("title", ""),
        "status": meta.get("status"),
        "mp4_key": meta.get("mp4_key"),
        "duration_sec": float(meta.get("duration_sec", 0) or 0),
        "failure_reason": meta.get("failure_reason"),
        "caption": caption,
        "hashtags": hashtags,
        "evaluation": evaluation,
    }


@app.patch("/shorts/{short_id}/caption")
def update_short_caption(short_id: str, body: UpdateShortCaptionRequest) -> dict[str, Any]:
    """Update the YouTube description (caption) stored in the latest RENDER# item."""
    pk = _short_pk(short_id)
    render_item = _latest("RENDER#", pk)
    if not render_item:
        raise HTTPException(404, "no render found for this short")
    try:
        sp_raw = render_item.get("screenplay")
        sp = json.loads(sp_raw) if isinstance(sp_raw, str) else (sp_raw or {})
        sp["caption"] = body.caption
        _ddb.update_item(
            TableName=TABLE,
            Key={"pk": {"S": pk}, "sk": {"S": render_item["sk"]}},
            UpdateExpression="SET screenplay = :sp",
            ExpressionAttributeValues={":sp": {"S": json.dumps(sp)}},
        )
    except Exception as e:
        raise HTTPException(500, f"failed to update caption: {e}")
    return {"short_id": short_id, "caption": body.caption}


@app.get("/shorts/{short_id}/screenplay")
def get_short_screenplay(short_id: str) -> dict[str, Any]:
    """Return the latest screenplay (beats + shots) for display and editing."""
    pk = _short_pk(short_id)
    render_item = _latest("RENDER#", pk)
    if not render_item:
        raise HTTPException(404, "no render found for this short")
    sp_raw = render_item.get("screenplay")
    if not sp_raw:
        raise HTTPException(404, "screenplay not yet generated")
    sp = json.loads(sp_raw) if isinstance(sp_raw, str) else sp_raw
    return {"short_id": short_id, "screenplay": sp}


@app.patch("/shorts/{short_id}/screenplay/beats")
def update_short_beats(short_id: str, body: UpdateBeatsRequest) -> dict[str, Any]:
    """Overwrite the beats array in the latest screenplay (user-edited visuals)."""
    pk = _short_pk(short_id)
    render_item = _latest("RENDER#", pk)
    if not render_item:
        raise HTTPException(404, "no render found for this short")
    try:
        sp_raw = render_item.get("screenplay")
        sp = json.loads(sp_raw) if isinstance(sp_raw, str) else (sp_raw or {})
        sp["beats"] = body.beats
        new_sp_json = json.dumps(sp)
        _ddb.update_item(
            Key={"pk": pk, "sk": render_item["sk"]},
            UpdateExpression="SET screenplay = :sp",
            ExpressionAttributeValues={":sp": new_sp_json},
        )
    except Exception as e:
        raise HTTPException(500, f"failed to update beats: {e}")
    return {"short_id": short_id, "saved": True}


@app.post("/shorts/{short_id}/rerender")
def rerender_short(short_id: str) -> dict[str, Any]:
    """Re-render using the current (possibly user-edited) screenplay.
    Skips Screenwriter + Visual Director — goes straight to audio_slice + SFn."""
    meta = _load_short_meta(short_id)

    # META status can be stale (pack.py historically didn't write it back).
    # Sync from the latest RENDER# item before deciding whether to block.
    if meta.get("status") in ("GENERATING", "RENDERING"):
        pk = _short_pk(short_id)
        latest_render = _latest("RENDER#", pk)
        if latest_render:
            latest_render = _sync_render_status(latest_render, pk)
            synced = latest_render.get("status")
            if synced in ("READY", "RENDER_FAILED"):
                meta["status"] = synced  # META was stale — allow re-render

    if meta.get("status") in ("GENERATING", "RENDERING"):
        raise HTTPException(409, f"already {meta.get('status')}")

    pk = _short_pk(short_id)
    existing_render = _latest("RENDER#", pk)
    if not existing_render:
        raise HTTPException(404, "no screenplay found; run generate first")
    sp_raw = existing_render.get("screenplay")
    if not sp_raw:
        raise HTTPException(404, "screenplay not yet generated")

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Create new RENDER# item with screenplay pre-populated so _run_short_rerender
    # can load it without needing the screenplay in the Lambda payload.
    _ddb.put_item(
        Item={
            "pk": pk,
            "sk": f"RENDER#{version}",
            "status": "RENDERING",
            "screenplay": sp_raw if isinstance(sp_raw, str) else json.dumps(sp_raw),
            "created_at": _now_iso(),
        }
    )
    _ddb.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression="SET #s = :s REMOVE failure_reason",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "RENDERING"},
    )

    payload = {"internal_task": "short_rerender", "short_id": short_id, "version": version}
    if not SELF_FUNCTION_NAME:
        _run_short_rerender(short_id, version)
    else:
        _lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    return {"short_id": short_id, "version": version, "status": "RENDERING"}


@app.post("/shorts/{short_id}/generate")
def generate_short(short_id: str) -> dict[str, Any]:
    """Kick off Screenwriter → Visual Director → audio_slice → render pipeline.
    Runs async via Lambda self-invoke (avoids API Gateway 30s timeout)."""
    meta = _load_short_meta(short_id)
    meta = _sync_short_transcribe_status(meta)
    status = meta.get("status")
    if status == "TRANSCRIBING":
        raise HTTPException(409, "still transcribing; poll /shorts/{id}/status first")
    if status == "TRANSCRIBE_FAILED":
        raise HTTPException(422, f"transcription failed: {meta.get('failure_reason')}")
    if status in ("GENERATING", "RENDERING"):
        raise HTTPException(409, f"already {status}")
    if not meta.get("transcript_key"):
        raise HTTPException(500, "transcript missing")

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _ddb.put_item(
        Item={
            "pk": _short_pk(short_id),
            "sk": f"RENDER#{version}",
            "status": "GENERATING",
            "created_at": _now_iso(),
        }
    )
    _ddb.update_item(
        Key={"pk": _short_pk(short_id), "sk": "META"},
        UpdateExpression="SET #s = :s REMOVE failure_reason",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "GENERATING"},
    )

    payload = {"internal_task": "short_generate", "short_id": short_id, "version": version}
    if not SELF_FUNCTION_NAME:
        _run_short_generate(short_id, version)
    else:
        _lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    return {"short_id": short_id, "version": version, "status": "GENERATING"}


@app.get("/shorts")
def list_shorts() -> dict[str, Any]:
    resp = _ddb.query(
        IndexName="byType",
        KeyConditionExpression=Key("gsi1pk").eq("SHORTS"),
        ScanIndexForward=False,
    )
    return {
        "shorts": [
            {
                "short_id": item.get("short_id", ""),
                "title": item.get("title", ""),
                "status": item.get("status", "UNKNOWN"),
                "created_at": item.get("created_at", ""),
            }
            for item in resp.get("Items", [])
        ]
    }


_mangum = Mangum(app)


def handler(event, context):
    """Lambda entrypoint. Dispatches between:
      - HTTP requests from API Gateway (Mangum)
      - Internal async self-invokes ({internal_task: "ideate", ...})
    """
    global SELF_FUNCTION_NAME
    if not SELF_FUNCTION_NAME and getattr(context, "invoked_function_arn", ""):
        # ARN format: arn:aws:lambda:REGION:ACCOUNT:function:NAME[:QUAL]
        SELF_FUNCTION_NAME = context.invoked_function_arn.split(":")[6]

    if isinstance(event, dict):
        task = event.get("internal_task")
        if task == "ideate":
            _run_ideation(int(event["episode_id"]))
            return {"ok": True}
        if task == "script":
            _run_script_task(
                kind=event["kind"],
                episode_id=int(event["episode_id"]),
                rank=int(event["rank"]),
                version=event["version"],
                instruction=event.get("instruction", ""),
            )
            return {"ok": True}
        if task == "short_generate":
            _run_short_generate(event["short_id"], event["version"])
            return {"ok": True}
        if task == "short_rerender":
            _run_short_rerender(event["short_id"], event["version"])
            return {"ok": True}
    return _mangum(event, context)
