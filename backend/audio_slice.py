"""Slice scene audio from the original podcast using FFmpeg.

Two entry points:
  - `slice_scenes(...)` — callable, used inline by the API Lambda during
    POST /script (after the screenwriter runs).
  - `handler(event, _ctx)` — legacy Step Functions handler; kept for back-compat
    while the state machine is being updated, but no longer part of the happy path.

Strict verbatim: every scene slices from the source audio. Polly only fires in
a should-never-happen bug path (scene missing source_start/source_end) so the
pipeline doesn't hard-fail; we log `[polly-bug]` loudly so we notice.
"""
import json
import os
import subprocess
import tempfile
from typing import Any

import boto3

_s3 = boto3.client("s3")
_polly = boto3.client("polly")
BUCKET = os.environ["ASSETS_BUCKET"]

# FFmpeg shipped via Lambda layer at /opt/bin/ffmpeg
FFMPEG = "/opt/bin/ffmpeg"

# Polly used only in the bug-guard path (scene with no timestamps).
VOICE_ID = os.environ.get("POLLY_VOICE", "Stephen")
ENGINE = os.environ.get("POLLY_ENGINE", "generative")


def _presign(key: str) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 2,
    )


def _slice(source_local: str, start: float, end: float, out_path: str) -> None:
    duration = max(0.1, float(end) - float(start))
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-ss", f"{float(start):.3f}",
            "-i", source_local,
            "-t", f"{duration:.3f}",
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "128k",
            out_path,
        ],
        check=True,
        capture_output=True,
    )


def _tts_bug_fallback(text: str, out_path: str) -> None:
    resp = _polly.synthesize_speech(
        Text=text, VoiceId=VOICE_ID, Engine=ENGINE, OutputFormat="mp3"
    )
    with open(out_path, "wb") as f:
        f.write(resp["AudioStream"].read())


def slice_scenes(
    *,
    episode_id: int | str,
    idea_rank: int,
    version: str,
    script: dict[str, Any],
    source_audio_key: str,
) -> list[dict[str, Any]]:
    """Download source once, slice each scene into its own MP3, upload to S3,
    return a list of `{index, audio_key, audio_url, source}` ready for Remotion."""
    with tempfile.TemporaryDirectory(prefix="audio-slice-") as work:
        source_local = os.path.join(work, "source")
        _s3.download_file(BUCKET, source_audio_key, source_local)

        scene_audio: list[dict[str, Any]] = []
        # Support new beats[] and legacy scenes[] layout.
        segments = script.get("beats") or script.get("scenes") or []
        for i, scene in enumerate(segments):
            out_key = (
                f"episodes/{episode_id}/idea-{idea_rank}/scripts/{version}/tts/scene_{i:02d}.mp3"
            )
            out_local = os.path.join(work, f"scene_{i:02d}.mp3")
            src_start = scene.get("source_start")
            src_end = scene.get("source_end")

            if src_start is not None and src_end is not None:
                _slice(source_local, src_start, src_end, out_local)
                source_used = "original"
            else:
                # Strict-verbatim screenwriter should never produce this; if it
                # does, we synthesize so the pipeline doesn't hard-fail and
                # flag it LOUDLY so we fix the prompt.
                print(f"[polly-bug] scene {i} missing source timestamps — synthesizing")
                _tts_bug_fallback(scene["voiceover"], out_local)
                source_used = "polly-bug"

            with open(out_local, "rb") as f:
                _s3.put_object(
                    Bucket=BUCKET,
                    Key=out_key,
                    Body=f.read(),
                    ContentType="audio/mpeg",
                )
            scene_audio.append(
                {
                    "index": i,
                    "audio_key": out_key,
                    "audio_url": _presign(out_key),
                    "source": source_used,
                }
            )
        return scene_audio


# ---------- Legacy Step Functions handler (kept temporarily) ----------


def handler(event: dict[str, Any], _ctx) -> dict[str, Any]:
    script = json.loads(_s3.get_object(Bucket=BUCKET, Key=event["script_s3_key"])["Body"].read())
    scene_audio = slice_scenes(
        episode_id=event["episode_id"],
        idea_rank=event["idea_rank"],
        version=event["version"],
        script=script,
        source_audio_key=event["source_audio_key"],
    )
    return {**event, "scene_audio": scene_audio}
