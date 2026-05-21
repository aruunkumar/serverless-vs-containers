#!/bin/bash
set -euo pipefail

###############################################################################
# deploy_fargate.sh
#
# Deploys 16 Fargate services (4 archetypes × 2 memory levels × 2 image sizes)
# behind ALB path-based routing rules.
#
# For each service:
#   1. Create CloudWatch log group at /ecs/{service-name}
#   2. Register task definition with awsvpc networking and correct CPU/memory
#   3. Create ALB target group with /health check
#   4. Add path-based listener rule routing /{service-name}/* to target group
#   5. Create ECS service with desired count 1
#   6. Record endpoint URL to endpoints.txt
#
# Naming: svc-{archetype}-{memory}-{imagesize}-container
# CPU/Memory mapping:
#   512MB  → 512 CPU units,  1024 MB task memory
#   2048MB → 2048 CPU units, 4096 MB task memory
#
# Prerequisites:
#   - experiment-env.sh with VPC, IAM, storage, ECS/ALB, and DocumentDB variables
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
for VAR in VPC_ID SUBNET_PRIV_A SUBNET_PRIV_B SG_ID FARGATE_ROLE_ARN BUCKET_NAME ACCOUNT_ID ECR_REGISTRY ALB_DNS LISTENER_ARN; do
  if [[ -z "${!VAR:-}" ]]; then
    echo "ERROR: $VAR not set in $ENV_FILE."
    exit 1
  fi
done

ARCHETYPES=(event-driven-api batch-transform ml-inference enterprise-microservice)
MEMORY_LABELS=(512mb 2gb)
IMAGE_SIZES=(slim standard)

DEPLOYED=0
PRIORITY=0

echo "=== Deploying 16 Fargate services ==="

for ARCHETYPE in "${ARCHETYPES[@]}"; do
  for MEM_LABEL in "${MEMORY_LABELS[@]}"; do
    for IMG_SIZE in "${IMAGE_SIZES[@]}"; do

      # Map memory label to CPU/memory values
      case "$MEM_LABEL" in
        512mb)
          TASK_CPU="512"
          TASK_MEM="1024"
          ;;
        2gb)
          TASK_CPU="2048"
          TASK_MEM="4096"
          ;;
      esac

      SVC_NAME="svc-${ARCHETYPE}-${MEM_LABEL}-${IMG_SIZE}-container"
      IMG_TAG="container-${IMG_SIZE}"
      IMAGE_URI="${ECR_REGISTRY}/svc-experiment/${ARCHETYPE}:${IMG_TAG}"
      PRIORITY=$((PRIORITY + 1))

      echo ""
      echo "--- Deploying ${SVC_NAME} (CPU=${TASK_CPU}, MEM=${TASK_MEM}, priority=${PRIORITY}) ---"

      # Build environment variables JSON array
      ENV_JSON='[
        {"name":"DATA_BUCKET","value":"'"${BUCKET_NAME}"'"},
        {"name":"PLATFORM","value":"fargate"},
        {"name":"ARCHETYPE","value":"'"${ARCHETYPE}"'"}
      ]'
      if [[ "$ARCHETYPE" == "enterprise-microservice" ]]; then
        if [[ -z "${DOCDB_ENDPOINT:-}" ]]; then
          echo "ERROR: DOCDB_ENDPOINT not set in $ENV_FILE. Run 05_setup_docdb.sh first."
          exit 1
        fi
        ENV_JSON='[
          {"name":"DATA_BUCKET","value":"'"${BUCKET_NAME}"'"},
          {"name":"PLATFORM","value":"fargate"},
          {"name":"ARCHETYPE","value":"'"${ARCHETYPE}"'"},
          {"name":"DOCDB_ENDPOINT","value":"'"${DOCDB_ENDPOINT}"'"},
          {"name":"DOCDB_USERNAME","value":"masteruser"},
          {"name":"DOCDB_PASSWORD","value":"ExperimentPass123"}
        ]'
      fi

      # Step 1: Create CloudWatch log group
      echo "  Creating CloudWatch log group..."
      aws logs create-log-group \
        --log-group-name "/ecs/${SVC_NAME}" \
        --region "$REGION" 2>/dev/null || echo "  Log group /ecs/${SVC_NAME} already exists."

      # Step 2: Register task definition
      echo "  Registering task definition..."
      TASK_DEF_JSON=$(cat <<EOF
{
  "family": "${SVC_NAME}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEM}",
  "executionRoleArn": "${FARGATE_ROLE_ARN}",
  "taskRoleArn": "${FARGATE_ROLE_ARN}",
  "containerDefinitions": [{
    "name": "${SVC_NAME}",
    "image": "${IMAGE_URI}",
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    "environment": ${ENV_JSON},
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/${SVC_NAME}",
        "awslogs-region": "${REGION}",
        "awslogs-stream-prefix": "ecs"
      }
    }
  }]
}
EOF
)
      echo "$TASK_DEF_JSON" > "/tmp/task-def-${SVC_NAME}.json"
      aws ecs register-task-definition \
        --cli-input-json "file:///tmp/task-def-${SVC_NAME}.json" \
        --region "$REGION" \
        --no-cli-pager

      # Step 3: Create ALB target group (skip if exists)
      echo "  Creating ALB target group..."
      case "$ARCHETYPE" in
        event-driven-api)          ARCH_ABBR="eda" ;;
        batch-transform)           ARCH_ABBR="bt" ;;
        ml-inference)              ARCH_ABBR="mli" ;;
        enterprise-microservice)   ARCH_ABBR="ems" ;;
      esac
      TG_NAME="svc-${ARCH_ABBR}-${MEM_LABEL}-${IMG_SIZE}-ctr"

      # Check if service already exists
      EXISTING_SVC=$(aws ecs describe-services \
        --cluster svc-experiment-cluster \
        --services "$SVC_NAME" \
        --region "$REGION" \
        --query 'services[?status==`ACTIVE`].serviceName' --output text 2>/dev/null || echo "")

      if [[ -n "$EXISTING_SVC" && "$EXISTING_SVC" != "None" ]]; then
        echo "  Service ${SVC_NAME} already exists — updating task definition and forcing new deployment..."
        aws ecs update-service \
          --cluster svc-experiment-cluster \
          --service "$SVC_NAME" \
          --task-definition "$SVC_NAME" \
          --force-new-deployment \
          --region "$REGION" \
          --no-cli-pager

        # Record endpoint if not already in endpoints.txt
        ENDPOINT="http://${ALB_DNS}/${SVC_NAME}/invoke"
        if ! grep -q "^${SVC_NAME}=" endpoints.txt 2>/dev/null; then
          echo "${SVC_NAME}=${ENDPOINT}" >> endpoints.txt
        fi

        DEPLOYED=$((DEPLOYED + 1))
        echo "  ✓ ${SVC_NAME} → ${ENDPOINT} (updated)"
        rm -f "/tmp/task-def-${SVC_NAME}.json"
        continue
      fi

      TG_ARN=$(aws elbv2 create-target-group \
        --name "$TG_NAME" \
        --protocol HTTP \
        --port 8080 \
        --vpc-id "$VPC_ID" \
        --target-type ip \
        --health-check-path "/health" \
        --region "$REGION" \
        --query 'TargetGroups[0].TargetGroupArn' --output text)

      # Step 4: Add path-based listener rule
      echo "  Adding listener rule (priority ${PRIORITY})..."
      aws elbv2 create-rule \
        --listener-arn "$LISTENER_ARN" \
        --priority "$PRIORITY" \
        --conditions '[{"Field":"path-pattern","Values":["/'${SVC_NAME}'/*"]}]' \
        --actions '[{"Type":"forward","TargetGroupArn":"'"${TG_ARN}"'"}]' \
        --region "$REGION" \
        --no-cli-pager

      # Step 5: Create ECS service
      echo "  Creating ECS service..."
      aws ecs create-service \
        --cluster svc-experiment-cluster \
        --service-name "$SVC_NAME" \
        --task-definition "$SVC_NAME" \
        --desired-count 1 \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_PRIV_A},${SUBNET_PRIV_B}],securityGroups=[${SG_ID}],assignPublicIp=DISABLED}" \
        --load-balancers "targetGroupArn=${TG_ARN},containerName=${SVC_NAME},containerPort=8080" \
        --region "$REGION" \
        --no-cli-pager

      # Step 6: Record endpoint URL
      ENDPOINT="http://${ALB_DNS}/${SVC_NAME}/invoke"
      echo "${SVC_NAME}=${ENDPOINT}" >> endpoints.txt

      # Clean up temp file
      rm -f "/tmp/task-def-${SVC_NAME}.json"

      DEPLOYED=$((DEPLOYED + 1))
      echo "  ✓ ${SVC_NAME} → ${ENDPOINT}"

    done
  done
done

echo ""
echo "============================================================"
echo "  Fargate Deployment Summary"
echo "============================================================"
echo "Successfully deployed: ${DEPLOYED}/16"
if [[ $DEPLOYED -eq 16 ]]; then
  echo "=== All 16 Fargate services deployed successfully ==="
else
  echo "WARNING: Expected 16 deployments, got ${DEPLOYED}."
fi
