#!/bin/bash
set -euo pipefail

###############################################################################
# deploy_lambda.sh
#
# Deploys 16 Lambda functions (4 archetypes × 2 memory levels × 2 image sizes)
# behind API Gateway HTTP APIs.
#
# For each function:
#   1. Create Lambda function from ECR image
#   2. Wait for function to become Active
#   3. Create API Gateway HTTP API with POST /invoke and GET /health routes
#   4. Create prod stage with auto-deploy
#   5. Grant API Gateway permission to invoke the function
#   6. Record endpoint URL to endpoints.txt
#
# Naming: svc-{archetype}-{memory}-{imagesize}-serverless
# Memory mapping: 512mb → 512 MB, 2gb → 2048 MB
#
# Prerequisites:
#   - experiment-env.sh with VPC, IAM, storage, and DocumentDB variables
#   - ECR images pushed (run build_and_push_images.sh first)
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run infrastructure setup scripts first."
  exit 1
fi
source "$ENV_FILE"

# Validate required variables
for VAR in SUBNET_PRIV_A SUBNET_PRIV_B SG_ID LAMBDA_ROLE_ARN BUCKET_NAME ACCOUNT_ID ECR_REGISTRY; do
  if [[ -z "${!VAR:-}" ]]; then
    echo "ERROR: $VAR not set in $ENV_FILE."
    exit 1
  fi
done

ARCHETYPES=(event-driven-api batch-transform ml-inference enterprise-microservice)
MEMORY_LABELS=(512mb 2gb)
IMAGE_SIZES=(slim standard)

DEPLOYED=0

echo "=== Deploying 16 Lambda functions ==="

for ARCHETYPE in "${ARCHETYPES[@]}"; do
  for MEM_LABEL in "${MEMORY_LABELS[@]}"; do
    for IMG_SIZE in "${IMAGE_SIZES[@]}"; do

      # Map memory label to MB value
      case "$MEM_LABEL" in
        512mb) MEMORY_MB=512 ;;
        2gb)   MEMORY_MB=2048 ;;
      esac

      FUNC_NAME="svc-${ARCHETYPE}-${MEM_LABEL}-${IMG_SIZE}-serverless"
      IMG_TAG="serverless-${IMG_SIZE}"
      IMAGE_URI="${ECR_REGISTRY}/svc-experiment/${ARCHETYPE}:${IMG_TAG}"

      echo ""
      echo "--- Deploying ${FUNC_NAME} (${MEMORY_MB}MB, ${IMG_TAG}) ---"

      # Build environment variables
      ENV_VARS="DATA_BUCKET=${BUCKET_NAME},PLATFORM=lambda,ARCHETYPE=${ARCHETYPE}"
      if [[ "$ARCHETYPE" == "enterprise-microservice" ]]; then
        if [[ -z "${DOCDB_ENDPOINT:-}" ]]; then
          echo "ERROR: DOCDB_ENDPOINT not set in $ENV_FILE. Run 05_setup_docdb.sh first."
          exit 1
        fi
        ENV_VARS="${ENV_VARS},DOCDB_ENDPOINT=${DOCDB_ENDPOINT},DOCDB_USERNAME=masteruser,DOCDB_PASSWORD=ExperimentPass123"
      fi

      # Step 1: Create or update the Lambda function
      echo "  Creating/updating Lambda function..."
      if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" &>/dev/null; then
        # Function exists — update code and configuration
        aws lambda update-function-code \
          --function-name "$FUNC_NAME" \
          --image-uri "${IMAGE_URI}" \
          --region "$REGION" \
          --no-cli-pager

        # Wait for update to complete before changing config
        aws lambda wait function-updated-v2 \
          --function-name "$FUNC_NAME" \
          --region "$REGION"

        aws lambda update-function-configuration \
          --function-name "$FUNC_NAME" \
          --memory-size "$MEMORY_MB" \
          --timeout 300 \
          --vpc-config "SubnetIds=${SUBNET_PRIV_A},${SUBNET_PRIV_B},SecurityGroupIds=${SG_ID}" \
          --environment "Variables={${ENV_VARS}}" \
          --region "$REGION" \
          --no-cli-pager

        echo "  Updated existing function."
      else
        # Function doesn't exist — create it
        aws lambda create-function \
          --function-name "$FUNC_NAME" \
          --package-type Image \
          --code "ImageUri=${IMAGE_URI}" \
          --role "$LAMBDA_ROLE_ARN" \
          --memory-size "$MEMORY_MB" \
          --timeout 300 \
          --vpc-config "SubnetIds=${SUBNET_PRIV_A},${SUBNET_PRIV_B},SecurityGroupIds=${SG_ID}" \
          --environment "Variables={${ENV_VARS}}" \
          --region "$REGION" \
          --no-cli-pager
      fi

      # Wait for function to become Active
      echo "  Waiting for function to become Active..."
      aws lambda wait function-active-v2 \
        --function-name "$FUNC_NAME" \
        --region "$REGION"

      # Step 2: Create API Gateway HTTP API (skip if endpoint already recorded)
      if grep -q "^${FUNC_NAME}=" endpoints.txt 2>/dev/null; then
        echo "  API Gateway already exists for ${FUNC_NAME}, skipping."
        ENDPOINT=$(grep "^${FUNC_NAME}=" endpoints.txt | head -1 | cut -d= -f2-)
      else
        echo "  Creating API Gateway HTTP API..."
        API_ID=$(aws apigatewayv2 create-api \
          --name "${FUNC_NAME}-api" \
          --protocol-type HTTP \
          --region "$REGION" \
          --query 'ApiId' --output text)

      LAMBDA_ARN=$(aws lambda get-function \
        --function-name "$FUNC_NAME" \
        --region "$REGION" \
        --query 'Configuration.FunctionArn' --output text)

      # Create Lambda integration with payload format 2.0
      INT_ID=$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$LAMBDA_ARN" \
        --payload-format-version "2.0" \
        --region "$REGION" \
        --query 'IntegrationId' --output text)

      # Create POST /invoke route (IAM auth — private, no public access)
      aws apigatewayv2 create-route \
        --api-id "$API_ID" \
        --route-key "POST /invoke" \
        --target "integrations/${INT_ID}" \
        --authorization-type AWS_IAM \
        --region "$REGION" \
        --no-cli-pager

      # Create GET /health route (IAM auth — private, no public access)
      aws apigatewayv2 create-route \
        --api-id "$API_ID" \
        --route-key "GET /health" \
        --target "integrations/${INT_ID}" \
        --authorization-type AWS_IAM \
        --region "$REGION" \
        --no-cli-pager

      # Step 3: Create prod stage with auto-deploy
      echo "  Creating prod stage with auto-deploy..."
      aws apigatewayv2 create-stage \
        --api-id "$API_ID" \
        --stage-name prod \
        --auto-deploy \
        --region "$REGION" \
        --no-cli-pager

      # Step 4: Grant API Gateway permission to invoke Lambda
      echo "  Granting API Gateway invoke permission..."
      aws lambda add-permission \
        --function-name "$FUNC_NAME" \
        --statement-id "apigateway-${API_ID}" \
        --action lambda:InvokeFunction \
        --principal apigateway.amazonaws.com \
        --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
        --region "$REGION" \
        --no-cli-pager

      # Step 5: Record endpoint URL
      ENDPOINT="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/invoke"
      echo "${FUNC_NAME}=${ENDPOINT}" >> endpoints.txt
      fi

      DEPLOYED=$((DEPLOYED + 1))
      echo "  ✓ ${FUNC_NAME} → ${ENDPOINT}"

    done
  done
done

echo ""
echo "============================================================"
echo "  Lambda Deployment Summary"
echo "============================================================"
echo "Successfully deployed: ${DEPLOYED}/16"
if [[ $DEPLOYED -eq 16 ]]; then
  echo "=== All 16 Lambda functions deployed successfully ==="
else
  echo "WARNING: Expected 16 deployments, got ${DEPLOYED}."
fi
