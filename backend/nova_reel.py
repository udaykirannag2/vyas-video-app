"""Amazon Nova Reel helper — async text-to-video generation on Bedrock.

Used as a secondary b-roll source when Pexels returns no usable candidates
for a scene. Generates a single 6-second portrait (720x1280) clip from the
scene's `visual` direction.

Pricing (us-east-1, on-demand): ~$0.08/second → ~$0.48 per 6s clip.
"""
import os
import time
from typing import Any

import boto3

_bedrock = boto3.client("bedrock-runtime")
_s3 = boto3.client("s3")

NOVA_REEL_MODEL = os.environ.get("NOVA_REEL_MODEL", "amazon.nova-reel-v1:1")
# Portrait 9:16 for vertical reels
DIMENSION = "720x1280"
DURATION_SECONDS = 6
FPS = 24


def start(prompt: str, output_bucket: str, output_prefix: str) -> str:
    """Fire an async Nova Reel job. Returns the invocationArn (used for polling).

    Nova writes `<output_prefix>/<invocation-id>/output.mp4` into the bucket.
    """
    s3_uri = f"s3://{output_bucket}/{output_prefix.rstrip('/')}/"
    resp = _bedrock.start_async_invoke(
        modelId=NOVA_REEL_MODEL,
        modelInput={
            "taskType": "TEXT_VIDEO",
            "textToVideoParams": {"text": prompt[:512]},
            "videoGenerationConfig": {
                "durationSeconds": DURATION_SECONDS,
                "fps": FPS,
                "dimension": DIMENSION,
            },
        },
        outputDataConfig={"s3OutputDataConfig": {"s3Uri": s3_uri}},
    )
    return resp["invocationArn"]


def wait(invocation_arn: str, *, timeout_sec: int = 600, poll_every: int = 10) -> dict[str, Any]:
    """Poll a Nova job until it's in a terminal state. Returns the full response.
    Raises TimeoutError on timeout, RuntimeError on Failed."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = _bedrock.get_async_invoke(invocationArn=invocation_arn)
        status = r.get("status")
        if status == "Completed":
            return r
        if status == "Failed":
            raise RuntimeError(
                f"Nova Reel job failed: {r.get('failureMessage', 'unknown')}"
            )
        time.sleep(poll_every)
    raise TimeoutError(f"Nova Reel job did not finish within {timeout_sec}s: {invocation_arn}")


def output_key(response: dict[str, Any]) -> str:
    """Extract the S3 key of the generated MP4 from a completed get_async_invoke response."""
    out_cfg = response.get("outputDataConfig", {}).get("s3OutputDataConfig", {})
    s3_uri = out_cfg.get("s3Uri", "")
    # s3Uri points at the output directory Nova wrote to, e.g.
    #   s3://<bucket>/<prefix>/<invocation-id>/
    # The actual video is at <s3Uri>/output.mp4.
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Unexpected Nova output s3Uri: {s3_uri!r}")
    _, _, rest = s3_uri[5:].partition("/")
    return rest.rstrip("/") + "/output.mp4"
