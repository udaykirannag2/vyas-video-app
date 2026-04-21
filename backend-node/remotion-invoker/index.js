// Step Functions handler: render a Remotion composition on Lambda, wait until
// done, then copy the MP4 from Remotion's bucket into our assets bucket.
const {
  renderMediaOnLambda,
  getRenderProgress,
} = require("@remotion/lambda/client");
const {
  S3Client,
  CopyObjectCommand,
  GetObjectCommand,
} = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");

const REGION = process.env.AWS_REGION || "us-east-1";
const FUNCTION_NAME = process.env.REMOTION_FUNCTION_NAME;
const SERVE_URL = process.env.REMOTION_SERVE_URL;
const ASSETS_BUCKET = process.env.ASSETS_BUCKET;
// Branded outro clip appended to every reel (4.88s, 720x1280, with audio).
const OUTRO_KEY = process.env.OUTRO_KEY || "shared/outro-v1.mp4";
const OUTRO_DURATION_SEC = Number(process.env.OUTRO_DURATION_SEC || "4.88");

if (!FUNCTION_NAME) throw new Error("REMOTION_FUNCTION_NAME not set");
if (!SERVE_URL) throw new Error("REMOTION_SERVE_URL not set");
if (!ASSETS_BUCKET) throw new Error("ASSETS_BUCKET not set");

const s3 = new S3Client({ region: REGION });

exports.handler = async (event) => {
  const { episode_id, idea_rank, version } = event;
  const prefix = `episodes/${episode_id}/idea-${idea_rank}/render-${version}`;
  const outputKey = `${prefix}/final.mp4`;

  // Presign the outro so Remotion's headless Chromium can GET it (the
  // assets bucket is private).
  const outroUrl = await getSignedUrl(
    s3,
    new GetObjectCommand({ Bucket: ASSETS_BUCKET, Key: OUTRO_KEY }),
    { expiresIn: 2 * 60 * 60 },
  );

  // Build the Remotion inputProps the <Reel /> composition expects.
  const inputProps = {
    script: await fetchScript(event),
    sceneAudio: event.scene_audio || [],
    sceneBroll: event.scene_broll || [],
    assetsBucket: ASSETS_BUCKET,
    projectId: `${episode_id}/idea-${idea_rank}`,
    // Multi-shot broll (new) + legacy compat
    shotBroll: event.shot_broll || event.scene_broll || [],
    sceneBroll: event.scene_broll || event.shot_broll || [],
    // Outro clip appended to every reel
    outroUrl,
    outroDurationSec: OUTRO_DURATION_SEC,
  };

  // 1. Kick off render — SDK handles all version/payload wiring.
  const { renderId, bucketName } = await renderMediaOnLambda({
    region: REGION,
    functionName: FUNCTION_NAME,
    serveUrl: SERVE_URL,
    composition: "Reel",
    inputProps,
    codec: "h264",
    imageFormat: "jpeg",
    outName: outputKey,
    privacy: "private",
    maxRetries: 1,
  });
  console.log("renderId", renderId, "bucket", bucketName);

  // 2. Poll until done or a 12-minute soft deadline.
  const deadline = Date.now() + 12 * 60 * 1000;
  let progress;
  while (Date.now() < deadline) {
    progress = await getRenderProgress({
      region: REGION,
      functionName: FUNCTION_NAME,
      renderId,
      bucketName,
    });
    if (progress.fatalErrorEncountered) {
      throw new Error(
        "Remotion fatal error: " +
          JSON.stringify(progress.errors, null, 2).slice(0, 1000),
      );
    }
    if (progress.done) break;
    await new Promise((r) => setTimeout(r, 4000));
  }
  if (!progress || !progress.done) {
    throw new Error(`Remotion render did not finish in time (renderId=${renderId})`);
  }

  // 3. Copy Remotion's output file into our assets bucket at the canonical key.
  const outBucket = progress.outBucket || bucketName;
  const outKey = progress.outKey;
  if (!outKey) {
    throw new Error(`Remotion done but no outKey: ${JSON.stringify(progress)}`);
  }
  await s3.send(
    new CopyObjectCommand({
      Bucket: ASSETS_BUCKET,
      Key: outputKey,
      CopySource: `/${outBucket}/${encodeURIComponent(outKey)}`,
    }),
  );

  return {
    ...event,
    render_id: renderId,
    output_key: outputKey,
    remotion_out_bucket: outBucket,
    remotion_out_key: outKey,
  };
};

// The Step Functions pipeline already fetches the script JSON in tts/broll,
// but the render Lambda runs in its own invocation so we re-fetch here from S3.
const { GetObjectCommand } = require("@aws-sdk/client-s3");

async function fetchScript(event) {
  const { script_s3_key } = event;
  const res = await s3.send(
    new GetObjectCommand({ Bucket: ASSETS_BUCKET, Key: script_s3_key }),
  );
  const body = await streamToString(res.Body);
  return JSON.parse(body);
}

function streamToString(stream) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    stream.on("data", (c) => chunks.push(c));
    stream.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    stream.on("error", reject);
  });
}
