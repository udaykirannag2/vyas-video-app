# Vyas-Video

Turn Bhagavad Gita podcast audio into short-form reels for a global 15–35 audience. Multi-agent AWS pipeline: **upload → transcribe → ideate → script → render → publish**.

- **Web portal** with Cognito sign-in, sidebar navigation, polished dark UI
- **Multi-agent ideation**: Claude Opus 4.6 (topic detection) + Sonnet 4.6 (scoring + scripting) + Haiku 4.5 (visual polish)
- **Audio-first**: FFmpeg slices the host's *actual voice* from the podcast — no TTS
- **Nova Reel** AI-generated b-roll for hero shots, Pexels stock for secondary shots
- **Multi-shot beats**: each spoken segment has 2–4 cinematic visual clips (edited-sequence pacing)
- **3-minute cap** — reels always fit YouTube Shorts / Instagram Reels limits
- **Production guardrails**: per-run budgets, circuit breakers, retry logic, failure surfacing + retry UI

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full system design.

## Layout

```
infra/         AWS CDK (Python) — Auth, API, Render, Frontend stacks
backend/       Python Lambda handlers + Strands agents
backend-node/  Node Lambda for Remotion Lambda invocation
remotion/      Remotion compositions (9:16 MP4)
frontend/      Next.js (static export → S3 + CloudFront)
scripts/       Deploy helpers
```

## Prerequisites

- AWS account with Bedrock models auto-enabled (Claude Opus 4.6, Sonnet 4.6, Haiku 4.5, Nova Reel v1:1)
- Node 20+, Python 3.11+, Docker (for Lambda bundling), pnpm
- `aws-cdk` CLI, AWS CLI configured with an admin-capable profile
- A Pexels API key (free) stored in SSM

## AWS profile

Set your profile in the shell before deploying:

```bash
source ./scripts/env.sh   # sets VYAS_AWS_PROFILE, AWS_REGION=us-east-1
aws sts get-caller-identity
```

Or manually:

```bash
export AWS_PROFILE=YOUR_AWS_PROFILE
export AWS_REGION=us-east-1
```

## One-time setup

```bash
# 1. Pexels API key in SSM
aws ssm put-parameter \
  --name /vyas-video/pexels-api-key \
  --value <YOUR_PEXELS_KEY> \
  --type SecureString

# 2. CDK bootstrap (once per account/region)
cd infra
pip install -r requirements.txt
cdk bootstrap
```

## Deploy

```bash
# 1. All four stacks (auth, render, api, frontend)
cd infra && cdk deploy --all

# 2. Remotion Lambda + site (one-time per Remotion version)
cd ../remotion && pnpm install
pnpm exec remotion lambda functions deploy --memory=2048 --disk=2048 --timeout=300
pnpm exec remotion lambda sites create src/index.ts --site-name=vyas-video

# 3. Frontend build + deploy
cd ../frontend && pnpm install
# Pull outputs from CloudFormation
export API_URL=$(aws cloudformation describe-stacks --stack-name VyasVideoApi \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)
export POOL_ID=$(aws cloudformation describe-stacks --stack-name VyasVideoAuth \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
export CLIENT_ID=$(aws cloudformation describe-stacks --stack-name VyasVideoAuth \
  --query "Stacks[0].Outputs[?OutputKey=='WebClientId'].OutputValue" --output text)
export BUCKET=$(aws cloudformation describe-stacks --stack-name VyasVideoFrontend \
  --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text)
export DIST_ID=$(aws cloudformation describe-stack-resources --stack-name VyasVideoFrontend \
  --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId" \
  --output text)

NEXT_PUBLIC_API_URL=$API_URL \
NEXT_PUBLIC_COGNITO_USER_POOL_ID=$POOL_ID \
NEXT_PUBLIC_COGNITO_CLIENT_ID=$CLIENT_ID \
  pnpm build

aws s3 sync out/ s3://$BUCKET/ --delete
aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
```

## Using the portal

1. Open the CloudFront URL (from `VyasVideoFrontend.SiteUrl`)
2. Sign up with email + password, verify with the code mailed to you
3. Click **+ New Episode**, give it a number, upload a podcast MP3 (or paste an existing S3 key)
4. Wait for Transcribe → ideation — 3 ideas appear, each with a scored title, hook, twist, payoff
5. Open an idea → **Generate script** — agent writes a multi-shot screenplay and slices audio
6. Preview each beat's sliced audio in the UI
7. **Render video** — Step Functions pipeline kicks off (b-roll fetch → Remotion → pack). ~5-7 min for ~10 beats
8. Download the finished MP4, copy the caption and hashtags, upload to Shorts/Reels

## Local dev

```bash
cd remotion && pnpm start         # Remotion studio @ localhost:3000
cd frontend && pnpm dev           # Next.js @ localhost:3000
cd backend && pytest              # unit tests
```

## Cost

| Step | Cost (per episode) |
|---|---|
| Transcribe (one-time per episode) | ~$0.50 |
| Ideation (Opus + Sonnet) | ~$0.14 |
| Script + Visual Director + Slice (per idea) | ~$0.08 |
| Render (Nova Reel primary + Pexels secondary) | ~$4.80 per reel (10 Nova shots) |
| **Full reel** | **~$5-6** |

Hard budget cap per run: **$8** (enforced by `backend/guardrails.py`).

## Production safety

See `backend/guardrails.py`:

- **Run budgets**: max 20 LLM calls, 8 retries, $8/run, 30 steps
- **Per-step limits**: max 2 retries (transient errors only), 3-min timeout
- **Circuit breakers**: opens after 3 consecutive failures or 50% rolling failure rate
- **Loop prevention**: aborts on 4 identical outputs (stalled pipeline)
- **Failure surfacing**: `SCRIPT_FAILED` / `RENDER_FAILED` states shown in UI with retry buttons
- **Structured logs**: every step with model, attempt, elapsed time, estimated cost → CloudWatch
