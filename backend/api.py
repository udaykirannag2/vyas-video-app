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
from transcript_cleanup import cleanup as cleanup_transcript, segments_for_range
from audio_slice import slice_scenes
from guardrails import RunContext, GuardrailsConfig, GuardrailError, log as glog
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
        MAX_REEL_SEC = 180.0
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


MAX_REEL_DURATION_SEC = 180.0  # YouTube Shorts / Instagram Reels cap


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
            shot_total = sum(s.shot_duration_sec for s in beat.shots)
            if shot_total > 0 and abs(shot_total - dur) > 0.5:
                scale = dur / shot_total
                for s in beat.shots:
                    s.shot_duration_sec = round(s.shot_duration_sec * scale, 2)
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
    if not key.startswith("episodes/"):
        raise HTTPException(400, "key must be under episodes/")
    url = _s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600
    )
    return {"url": url}


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
    return _mangum(event, context)
