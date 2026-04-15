# Vyas-Video

Internal tool that turns Bhagavad Gita podcast transcripts into short-form reels for a global 15–35 audience. Multi-agent chat workflow: **Ideation → Script → Video → Publish**.

See [plan](../../.claude/plans/nested-snacking-stream.md) for the full design.

## Layout

```
infra/      AWS CDK (Python) — all resources tagged app=vyas-video
backend/    Lambda handlers, Strands agents (Opus 4.6 + Sonnet 4.6), render pipeline
remotion/   Remotion compositions (9:16 MP4)
frontend/   Next.js (static export → S3 + CloudFront)
```

## Prerequisites

- AWS account with Bedrock access — **Claude Opus 4.6** and **Claude Sonnet 4.6** enabled in `us-east-1`
- Node 20+, Python 3.11+, Docker (for Remotion Lambda container)
- `aws-cdk` CLI, `uv` or `pip`, `pnpm`
- Pexels API key (free) — stored in SSM, see below

## AWS profile

Everything deploys through the **`YOUR_AWS_PROFILE`** profile. Set it once in your shell:

```bash
export AWS_PROFILE=YOUR_AWS_PROFILE
export AWS_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-east-1
```

Or source the helper:

```bash
source ./scripts/env.sh
```

Verify before deploying:

```bash
aws sts get-caller-identity     # should show the payer account
```

## One-time setup

```bash
# Pexels API key for stock b-roll
aws ssm put-parameter \
  --name /vyas-video/pexels-api-key \
  --value <YOUR_PEXELS_KEY> \
  --type SecureString

# Bootstrap CDK in the target account/region
cd infra
pip install -r requirements.txt
cdk bootstrap
```

## Deploy

```bash
# 1. Infra (all three stacks, all resources tagged app=vyas-video)
cd infra && cdk deploy --all

# 2. Remotion Lambda + site
cd ../remotion && pnpm install
pnpm exec remotion lambda functions deploy
pnpm exec remotion lambda sites create --site-name vyas-video

# 3. Frontend
cd ../frontend
NEXT_PUBLIC_API_URL=$(aws cloudformation describe-stacks \
  --stack-name VyasVideoApi --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text) \
  pnpm install && pnpm build
aws s3 sync out/ s3://$(aws cloudformation describe-stacks \
  --stack-name VyasVideoFrontend --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text)/ --delete
```

## Local dev

```bash
cd remotion && pnpm start         # Remotion studio @ localhost:3000
cd frontend && pnpm dev           # Next.js @ localhost:3000
cd backend && pytest              # unit tests
```

## Cost target

< $0.25 per 30s reel. Idle cost ≈ $0 (serverless). Filter Cost Explorer by tag `app=vyas-video` to track spend.
