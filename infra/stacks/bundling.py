"""Shared Lambda code asset with Docker-based pip bundling.

We build one asset per stack and reuse it across every Lambda in that stack,
so `pip install` runs once (not N times).
"""
from aws_cdk import AssetHashType, BundlingOptions, aws_lambda as _lambda


def node_code(subdir: str) -> _lambda.AssetCode:
    """Bundle a Node Lambda from backend-node/<subdir>/ by running npm install
    inside the Node 20 SAM build image (ARM64)."""
    return _lambda.Code.from_asset(
        f"../backend-node/{subdir}",
        bundling=BundlingOptions(
            image=_lambda.Runtime.NODEJS_20_X.bundling_image,
            platform="linux/arm64",
            command=[
                "bash",
                "-c",
                # Copy source to a writable tmpdir, install deps there, ship result.
                "cp -au . /tmp/src && cd /tmp/src && "
                "npm install --omit=dev --no-audit --no-fund && "
                "cp -au . /asset-output",
            ],
        ),
    )


def ffmpeg_layer_code() -> _lambda.AssetCode:
    """Build a Lambda layer with an ARM64 static FFmpeg binary at /opt/bin/ffmpeg.

    Uses a CUSTOM asset hash so the layer isn't rebuilt (and its export ARN
    rotated) every time backend/ source changes — otherwise cross-stack
    imports from the API stack break with a "export in use" error.
    Bump `asset_hash` when we deliberately want a new FFmpeg build.
    """
    return _lambda.Code.from_asset(
        "../backend",  # contents don't matter; see CUSTOM hash below
        asset_hash_type=AssetHashType.CUSTOM,
        asset_hash="ffmpeg-layer-arm64-v1",
        bundling=BundlingOptions(
            image=_lambda.Runtime.PYTHON_3_11.bundling_image,
            platform="linux/arm64",
            command=[
                "bash",
                "-c",
                "mkdir -p /asset-output/bin && "
                "curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz | "
                "tar -xJ --strip-components=1 -C /tmp && "
                "cp /tmp/ffmpeg /tmp/ffprobe /asset-output/bin/ && "
                "chmod +x /asset-output/bin/ffmpeg /asset-output/bin/ffprobe",
            ],
        ),
    )


def backend_code() -> _lambda.AssetCode:
    # We target Lambda architecture ARM_64. The SAM Python 3.11 image runs natively
    # on ARM on Apple Silicon and on x86 CI; in both cases we force pip to resolve
    # manylinux ARM wheels so the Lambda runtime loads compiled modules correctly.
    return _lambda.Code.from_asset(
        "../backend",
        bundling=BundlingOptions(
            image=_lambda.Runtime.PYTHON_3_11.bundling_image,
            platform="linux/arm64",
            command=[
                "bash",
                "-c",
                "pip install --platform manylinux2014_aarch64 "
                "--implementation cp --python-version 3.11 --only-binary=:all: --upgrade "
                "-r requirements.txt -t /asset-output && cp -au . /asset-output",
            ],
        ),
    )
