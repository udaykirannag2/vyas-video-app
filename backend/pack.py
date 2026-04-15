"""Step Functions handler: write caption.txt, hashtags.txt, metadata.json
alongside the final MP4, and mark the render RENDER item READY."""
import json
import os
from typing import Any

import boto3

_s3 = boto3.client("s3")
_ddb = boto3.client("dynamodb")
BUCKET = os.environ["ASSETS_BUCKET"]
TABLE = os.environ.get("TABLE_NAME", "")


def handler(event: dict[str, Any], _ctx) -> dict[str, Any]:
    episode_id = event["episode_id"]
    idea_rank = event["idea_rank"]
    version = event["version"]
    script = json.loads(_s3.get_object(Bucket=BUCKET, Key=event["script_s3_key"])["Body"].read())

    prefix = f"episodes/{episode_id}/idea-{idea_rank}/render-{version}"
    _s3.put_object(Bucket=BUCKET, Key=f"{prefix}/caption.txt", Body=script["caption"].encode())
    _s3.put_object(
        Bucket=BUCKET,
        Key=f"{prefix}/hashtags.txt",
        Body=" ".join(script["hashtags"]).encode(),
    )
    _s3.put_object(
        Bucket=BUCKET,
        Key=f"{prefix}/metadata.json",
        Body=json.dumps(
            {
                "title": script["title"],
                "duration_sec": script["duration_sec"],
                "aspect": script["aspect"],
                "mp4_key": event["output_key"],
            }
        ).encode(),
    )

    if TABLE:
        _ddb.update_item(
            TableName=TABLE,
            Key={
                "pk": {"S": f"EPISODE#{episode_id}"},
                "sk": {"S": f"IDEA#{idea_rank}#RENDER#{version}"},
            },
            UpdateExpression="SET #s = :s, mp4_key = :m",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": {"S": "READY"},
                ":m": {"S": event["output_key"]},
            },
        )

    return {**event, "status": "READY"}
