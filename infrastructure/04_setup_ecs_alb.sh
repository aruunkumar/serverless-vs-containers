#!/bin/bash
set -euo pipefail

###############################################################################
# 04_setup_ecs_alb.sh
#
# Creates ECS cluster and internal ALB for the serverless-vs-container
# benchmark experiment in us-east-2:
#   - ECS cluster: svc-experiment-cluster with FARGATE capacity provider
#   - Internal ALB: svc-experiment-alb in private subnets
#   - Default HTTP listener on port 80 returning 404 fixed response
#
# Persists ALB_ARN, ALB_DNS, and LISTENER_ARN to experiment-env.sh.
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
# ECS Service-Linked Role (required on first-time ECS usage in the account)
###############################################################################
echo "=== Ensuring ECS service-linked role exists ==="

aws iam create-service-linked-role \
  --aws-service-name ecs.amazonaws.com 2>/dev/null \
  || echo "ECS service-linked role already exists."

# Brief wait for IAM propagation
sleep 5

###############################################################################
# ECS Cluster
###############################################################################
echo "=== Creating ECS Cluster ==="

aws ecs create-cluster \
  --cluster-name svc-experiment-cluster \
  --capacity-providers FARGATE \
  --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --region "$REGION"
echo "ECS cluster svc-experiment-cluster created."

###############################################################################
# Internal Application Load Balancer
###############################################################################
echo "=== Creating Internal ALB ==="

ALB_ARN=$(aws elbv2 create-load-balancer \
  --name svc-experiment-alb \
  --subnets "$SUBNET_PRIV_A" "$SUBNET_PRIV_B" \
  --security-groups "$SG_ID" \
  --scheme internal \
  --type application \
  --region "$REGION" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)
echo "ALB_ARN=$ALB_ARN"

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns "$ALB_ARN" \
  --region "$REGION" \
  --query 'LoadBalancers[0].DNSName' --output text)
echo "ALB_DNS=$ALB_DNS"

###############################################################################
# Default HTTP Listener (port 80 → 404 fixed response)
###############################################################################
echo "=== Creating Default HTTP Listener ==="

LISTENER_ARN=$(aws elbv2 create-listener \
  --load-balancer-arn "$ALB_ARN" \
  --protocol HTTP --port 80 \
  --default-actions 'Type=fixed-response,FixedResponseConfig={StatusCode="404",ContentType="text/plain",MessageBody="Not Found"}' \
  --region "$REGION" \
  --query 'Listeners[0].ListenerArn' --output text)
echo "LISTENER_ARN=$LISTENER_ARN"

###############################################################################
# Persist values
###############################################################################
echo "=== Persisting ECS/ALB values to $ENV_FILE ==="
{
  echo "ALB_ARN=$ALB_ARN"
  echo "ALB_DNS=$ALB_DNS"
  echo "LISTENER_ARN=$LISTENER_ARN"
} >> "$ENV_FILE"

echo "=== ECS and ALB setup complete ==="
echo "ALB endpoint: http://$ALB_DNS"
echo "All values saved to $ENV_FILE"
