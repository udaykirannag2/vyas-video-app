"""Step Functions handler: fetch b-roll for each scene.

Primary source: Pexels free stock video.
Secondary source: Amazon Nova Reel text-to-video (used only when Pexels returns
no usable candidate across all 3 alternate queries for a scene — keeps the
fallback cost bounded).

Each scene has up to 3 ordered `broll_queries` (with `broll_query` as back-compat
alias). Pexels filters: portrait orientation, HD resolution, duration >= scene
length. If Pexels misses, Nova Reel generates a single 6s portrait clip from
the scene's `visual` direction text.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import boto3
import requests

import nova_reel
from guardrails import RunContext, log as glog

_s3 = boto3.client("s3")
_ssm = boto3.client("ssm")
BUCKET = os.environ["ASSETS_BUCKET"]
PEXELS_KEY_PARAM = os.environ.get("PEXELS_KEY_PARAM", "/vyas-video/pexels-api-key")

SEARCH_URL = "https://api.pexels.com/videos/search"


def _pexels_key() -> str:
    val = os.environ.get("PEXELS_API_KEY")
    if val:
        return val
    return _ssm.get_parameter(Name=PEXELS_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]


def _search(query: str, headers: dict[str, str], per_page: int = 10) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            SEARCH_URL,
            headers=headers,
            params={"query": query, "orientation": "portrait", "per_page": per_page, "size": "medium"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[broll] pexels search failed for {query!r}: {e}")
        return []
    return r.json().get("videos", []) or []


def _portrait_file(video: dict[str, Any]) -> dict[str, Any] | None:
    """Pick a portrait-native HD file from a Pexels video entry, preferring
    1080p-ish quality. Returns None if nothing portrait-native at HD exists."""
    files = video.get("video_files", []) or []
    portrait = [f for f in files if f.get("height", 0) > f.get("width", 0)]
    if not portrait:
        return None
    hd = [f for f in portrait if f.get("height", 0) >= 1080]
    pool = hd or portrait
    # Closest to 1920 tall, then preferring hd/hls types.
    pool.sort(key=lambda f: (abs(f.get("height", 0) - 1920), 0 if f.get("quality") == "hd" else 1))
    return pool[0]


def _score(video: dict[str, Any], vf: dict[str, Any], scene_duration: float) -> float:
    """Higher is better. Combines: duration margin (clip longer than the scene
    is good), resolution (1080p preferred), portrait-native (native > cropped)."""
    dur = float(video.get("duration", 0))
    # We need the clip to cover the whole scene; a big negative if shorter.
    duration_score = 0.0 if dur >= scene_duration else -100.0 + (dur - scene_duration)
    if dur >= scene_duration:
        # Slight preference for "just enough" rather than too-long (avoids clips
        # where the interesting bit is buried in minute 3).
        duration_score = 10.0 - min(dur - scene_duration, 20.0) * 0.1
    h = vf.get("height", 0)
    res_score = min(h / 1080.0, 2.0) * 5.0  # 1080p ≈ 5, 2160p ≈ 10
    w = vf.get("width", 0)
    aspect_score = 5.0 if h > w else 0.0  # native portrait wins
    return duration_score + res_score + aspect_score


def _pick_best(queries: list[str], headers: dict[str, str], scene_duration: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Walk queries in order. Return (video, video_file, matched_query) or (None, None, '')."""
    for q in queries:
        if not q or not q.strip():
            continue
        videos = _search(q, headers)
        if not videos:
            continue
        candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for v in videos:
            vf = _portrait_file(v)
            if not vf:
                continue
            candidates.append((_score(v, vf, scene_duration), v, vf))
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[0], reverse=True)
        top_score, top_v, top_vf = candidates[0]
        # If the best candidate is worse than "barely usable", keep searching.
        if top_score < 0:
            continue
        return top_v, top_vf, q
    return None, None, ""


def _presign(key: str) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 2,
    )


def _download_pexels(vf_link: str, broll_key: str) -> bool:
    try:
        data = requests.get(vf_link, timeout=30).content
        _s3.put_object(Bucket=BUCKET, Key=broll_key, Body=data, ContentType="video/mp4")
        return True
    except requests.RequestException as e:
        print(f"[broll] pexels download failed: {e}")
        return False


def handler(event: dict[str, Any], _ctx) -> dict[str, Any]:
    ctx = RunContext()  # per-invocation run context for Nova calls
    episode_id = event["episode_id"]
    idea_rank = event.get("idea_rank")
    project_id = event.get("project_id", f"{episode_id}/idea-{idea_rank}")
    script = json.loads(_s3.get_object(Bucket=BUCKET, Key=event["script_s3_key"])["Body"].read())

    key = _pexels_key()
    headers = {"Authorization": key}
    scene_broll: list[dict[str, Any]] = [None] * len(script["scenes"])  # type: ignore
    pexels_fallbacks: list[tuple[int, dict[str, Any]]] = []

    # Pass 1: Nova Reel (PRIMARY) for every scene — generates video from the
    # scene's `visual` direction text. Higher cost but visuals match the
    # spiritual/metaphorical content far better than stock footage.
    nova_scenes: list[tuple[int, dict[str, Any]]] = [
        (i, scene) for i, scene in enumerate(script["scenes"])
    ]

    if nova_scenes:
        print(f"[broll] firing {len(nova_scenes)} Nova Reel job(s) (primary, staggered)")
        pending: dict[int, tuple[str, str]] = {}
        import time as _time
        for j, (i, scene) in enumerate(nova_scenes):
            if j > 0:
                _time.sleep(3)  # stagger to avoid concurrency throttle
            # Nova Reel gets the cinematic `visual` description (polished by the
            # visual director), not the Pexels search keywords.
            prompt_text = scene.get("visual") or (scene.get("voiceover") or "")[:200]
            beat = scene.get("beat_type", "build")
            nova_prefix = f"tmp/nova/{project_id}/scene_{i:02d}"
            try:
                ctx._check_budgets(f"nova.scene_{i}", estimated_cost=0.48)
                arn = nova_reel.start(prompt_text, BUCKET, nova_prefix, beat_type=beat)
                ctx.estimated_cost += 0.48
                ctx.llm_calls += 1
                pending[i] = (arn, nova_prefix)
                glog(f"[broll] nova start scene {i}: {arn}", cost=f"${ctx.estimated_cost:.2f}")
            except Exception as e:
                glog(f"[broll] nova start failed scene {i}: {e!r}")
                scene_broll[i] = {
                    "index": i, "broll_key": None, "broll_url": None,
                    "source": "none", "matched_query": None,
                }

        # Poll all Nova jobs concurrently.
        def _wait_and_copy(scene_index: int) -> dict[str, Any]:
            arn, _ = pending[scene_index]
            try:
                resp = nova_reel.wait(arn, timeout_sec=540)
                nova_key = nova_reel.output_key(resp)
                # Copy into the canonical broll path so render + frontend code
                # doesn't need to know about Nova's layout.
                broll_key = f"projects/{project_id}/broll/scene_{scene_index:02d}.mp4"
                _s3.copy_object(
                    Bucket=BUCKET,
                    Key=broll_key,
                    CopySource={"Bucket": BUCKET, "Key": nova_key},
                )
                return {
                    "index": scene_index,
                    "broll_key": broll_key,
                    "broll_url": _presign(broll_key),
                    "source": "nova-reel",
                    "matched_query": None,
                    "nova_invocation_arn": arn,
                }
            except Exception as e:
                print(f"[broll] nova wait/copy failed scene {scene_index}: {e!r}")
                return {
                    "index": scene_index, "broll_key": None, "broll_url": None,
                    "source": "nova-failed", "matched_query": None,
                }

        with ThreadPoolExecutor(max_workers=len(pending) or 1) as pool:
            futures = {pool.submit(_wait_and_copy, idx): idx for idx in pending}
            for fut in as_completed(futures):
                result = fut.result()
                scene_broll[result["index"]] = result
                if result.get("source") in ("nova-failed", None) and not result.get("broll_key"):
                    idx = result["index"]
                    pexels_fallbacks.append((idx, script["scenes"][idx]))

    # Pass 2: Pexels (SECONDARY) for any scenes where Nova failed.
    if pexels_fallbacks:
        print(f"[broll] {len(pexels_fallbacks)} Nova miss(es) — trying Pexels fallback")
        for i, scene in pexels_fallbacks:
            queries: list[str] = scene.get("broll_queries") or []
            if not queries and scene.get("broll_query"):
                queries = [scene["broll_query"]]
            scene_duration = max(1.0, float(scene.get("end", 0)) - float(scene.get("start", 0)))
            video, vf, matched = _pick_best(queries, headers, scene_duration)
            if video and vf:
                broll_key = f"projects/{project_id}/broll/scene_{i:02d}.mp4"
                if _download_pexels(vf["link"], broll_key):
                    scene_broll[i] = {
                        "index": i,
                        "broll_key": broll_key,
                        "broll_url": _presign(broll_key),
                        "source": "pexels-fallback",
                        "matched_query": matched,
                        "pexels_id": video.get("id"),
                    }

    return {**event, "scene_broll": scene_broll}
