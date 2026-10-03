#!/bin/bash
# scripts/deploy_lambda.sh
# Packages and deploys Lambda functions

set -euo pipefail

ENVIRONMENT=${1:-dev}
REGION=${AWS_DEFAULT_REGION:-us-east-1}
PROJECT_NAME="self-healing-infra"
BUILD_DIR="/tmp/lambda-build-$$"

echo "=== Deploying Lambda functions to $ENVIRONMENT ==="

# ─── Prerequisites ────────────────────────────────────────────────
command -v aws >/dev/null 2>&1 || { echo "aws CLI not found"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found"; exit 1; }
command -v zip >/dev/null 2>&1 || { echo "zip not found"; exit 1; }

# ─── Build Package ────────────────────────────────────────────────
echo "Building deployment package..."
mkdir -p "$BUILD_DIR"

# Install dependencies into package dir
pip install \
  boto3 requests PyYAML \
  --target "$BUILD_DIR" \
  --quiet \
  --platform manylinux2014_x86_64 \
  --only-binary=:all: \
  --python-version 3.11

# Copy source code
cp -r src/* "$BUILD_DIR/"

# Create zip
cd "$BUILD_DIR"
zip -r "/tmp/lambda-package.zip" . -q
cd -

PACKAGE_SIZE=$(du -sh /tmp/lambda-package.zip | cut -f1)
echo "Package size: $PACKAGE_SIZE"

# ─── Deploy Functions ─────────────────────────────────────────────
FUNCTIONS=(
  "health-checker:src/health_checker/main.py:lambda_handler"
  "healer:src/healer/main.py:lambda_handler"
)

for func_spec in "${FUNCTIONS[@]}"; do
  IFS=':' read -r func_suffix _ handler <<< "$func_spec"
  FUNCTION_NAME="${PROJECT_NAME}-${func_suffix}-${ENVIRONMENT}"

  echo "Deploying $FUNCTION_NAME..."

  # Check if function exists
  if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" &>/dev/null; then
    # Update existing
    aws lambda update-function-code \
      --function-name "$FUNCTION_NAME" \
      --zip-file "fileb:///tmp/lambda-package.zip" \
      --region "$REGION" \
      --output text \
      --query 'FunctionArn' \
      | xargs echo "  Updated:"
  else
    echo "  Function $FUNCTION_NAME not found. Deploy with Terraform first."
    echo "  cd terraform/environments/$ENVIRONMENT && terraform apply"
    exit 1
  fi

  # Wait for update to complete
  aws lambda wait function-updated \
    --function-name "$FUNCTION_NAME" \
    --region "$REGION" \
    2>/dev/null || true

  echo "  ✅ $FUNCTION_NAME deployed"
done

# ─── Smoke Test ──────────────────────────────────────────────────
echo ""
echo "Running smoke test on health-checker..."
RESULT=$(aws lambda invoke \
  --function-name "${PROJECT_NAME}-health-checker-${ENVIRONMENT}" \
  --payload '{"source": "smoke-test"}' \
  --region "$REGION" \
  /tmp/lambda-response.json \
  --output json 2>&1)

if grep -q '"StatusCode": 200' <<< "$RESULT"; then
  echo "  ✅ Smoke test passed"
  cat /tmp/lambda-response.json
else
  echo "  ❌ Smoke test failed: $RESULT"
  exit 1
fi

# ─── Cleanup ──────────────────────────────────────────────────────
rm -rf "$BUILD_DIR" /tmp/lambda-package.zip /tmp/lambda-response.json

echo ""
echo "=== Deployment complete ✅ ==="
echo "CloudWatch Dashboard: https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=${PROJECT_NAME}-${ENVIRONMENT}"
