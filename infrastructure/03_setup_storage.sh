#!/bin/bash
set -euo pipefail

###############################################################################
# 03_setup_storage.sh
#
# Creates storage resources for the serverless-vs-container benchmark:
#   - S3 bucket: svc-experiment-data-{ACCOUNT_ID} in us-east-2
#   - 4 ECR repositories (one per archetype):
#       svc-experiment/event-driven-api
#       svc-experiment/batch-transform
#       svc-experiment/ml-inference
#       svc-experiment/enterprise-microservice
#
# Persists BUCKET_NAME, ACCOUNT_ID, and ECR_REGISTRY to experiment-env.sh.
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

# Source the env file (must exist from previous scripts)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run 01_setup_vpc.sh first."
  exit 1
fi
source "$ENV_FILE"

###############################################################################
# S3 Bucket
###############################################################################
echo "=== Creating S3 Bucket ==="

ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
BUCKET_NAME="svc-experiment-data-${ACCOUNT_ID}"

aws s3api create-bucket \
  --bucket "$BUCKET_NAME" \
  --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
echo "BUCKET_NAME=$BUCKET_NAME"

###############################################################################
# ECR Repositories
###############################################################################
echo "=== Creating ECR Repositories ==="

for ARCHETYPE in event-driven-api batch-transform ml-inference enterprise-microservice; do
  aws ecr create-repository \
    --repository-name "svc-experiment/${ARCHETYPE}" \
    --region "$REGION"
  echo "Created ECR repo: svc-experiment/${ARCHETYPE}"
done

ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
echo "ECR_REGISTRY=$ECR_REGISTRY"

###############################################################################
# Persist values
###############################################################################
echo "=== Persisting storage values to $ENV_FILE ==="
{
  echo "BUCKET_NAME=$BUCKET_NAME"
  echo "ACCOUNT_ID=$ACCOUNT_ID"
  echo "ECR_REGISTRY=$ECR_REGISTRY"
} >> "$ENV_FILE"

echo "=== Storage setup complete ==="
echo "All storage values saved to $ENV_FILE"
