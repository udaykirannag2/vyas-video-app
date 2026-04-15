# Source this file to set up the AWS deploy environment for Vyas-Video.
#   source ./scripts/env.sh
#
# Set VYAS_AWS_PROFILE to your own AWS profile before sourcing, e.g.
#   export VYAS_AWS_PROFILE=my-profile   # or put it in ~/.zshrc

export AWS_PROFILE="${VYAS_AWS_PROFILE:-YOUR_AWS_PROFILE}"
export AWS_REGION=us-east-1
export CDK_DEFAULT_REGION=us-east-1
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"

if [ -z "$CDK_DEFAULT_ACCOUNT" ]; then
  echo "⚠️  Could not resolve account for profile $AWS_PROFILE."
  echo "   Run: aws sso login --profile $AWS_PROFILE   (or configure the profile)"
else
  echo "✅ AWS_PROFILE=$AWS_PROFILE  account=$CDK_DEFAULT_ACCOUNT  region=$AWS_REGION"
fi
