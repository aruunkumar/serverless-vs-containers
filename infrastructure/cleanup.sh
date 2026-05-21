#!/bin/bash
set -euo pipefail

###############################################################################
# cleanup.sh
#
# Deletes ALL experiment resources in reverse dependency order:
#   1.  Lambda functions (svc- prefix)
#   2.  API Gateway HTTP APIs (svc- prefix)
#   3.  ECS services (scale to 0, delete) + deregister task definitions
#   4.  ALB target groups (svc- prefix) + ALB listener + ALB
#   5.  DocumentDB cluster + instances + subnet group
#   6.  ECR repositories (svc-experiment/ with --force)
#   7.  S3 bucket (empty + delete)
#   8.  NAT Gateway + release EIP + detach/delete IGW
#   9.  Subnets + route tables + security group + VPC
#  10.  IAM roles (detach all policies + delete)
#  11.  EC2 instance + instance profile
#
# Sources experiment-env.sh for resource IDs.
# Uses || true on delete commands so the script continues if a resource is
# already deleted.
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

###############################################################################
# Pre-flight: source env file
###############################################################################
if [[ ! -f "$ENV_FILE" ]]; then
  echo "WARNING: $ENV_FILE not found. Will attempt cleanup using AWS API discovery."
else
  source "$ENV_FILE"
fi

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query 'Account' --output text)}"

###############################################################################
# Pre-flight: warn if results.csv not backed up
###############################################################################
echo "============================================================"
echo "  WARNING: Ensure results.csv has been backed up before"
echo "  proceeding. This script will delete ALL experiment"
echo "  resources including the S3 bucket."
echo "============================================================"
if [[ -f "results.csv" ]]; then
  echo "  results.csv found locally — verify it is backed up."
else
  echo "  results.csv NOT found locally — confirm it is saved elsewhere."
fi
echo ""
read -r -p "Type 'yes' to continue with cleanup: " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
  echo "Cleanup aborted."
  exit 0
fi

###############################################################################
# 1. Delete Lambda functions (svc- prefix)
###############################################################################
echo "=== 1/11 Deleting Lambda functions ==="

LAMBDA_FUNCTIONS=$(aws lambda list-functions \
  --region "$REGION" \
  --query 'Functions[?starts_with(FunctionName, `svc-`)].FunctionName' \
  --output text) || true

for FUNC in $LAMBDA_FUNCTIONS; do
  echo "  Deleting Lambda function: $FUNC"
  aws lambda delete-function --function-name "$FUNC" --region "$REGION" || true
done
echo "  Lambda function cleanup complete."

###############################################################################
# 2. Delete API Gateway HTTP APIs (svc- prefix)
###############################################################################
echo "=== 2/11 Deleting API Gateway HTTP APIs ==="

API_IDS=$(aws apigatewayv2 get-apis \
  --region "$REGION" \
  --query 'Items[?starts_with(Name, `svc-`)].ApiId' \
  --output text) || true

for API_ID in $API_IDS; do
  echo "  Deleting API Gateway: $API_ID"
  aws apigatewayv2 delete-api --api-id "$API_ID" --region "$REGION" || true
done
echo "  API Gateway cleanup complete."

###############################################################################
# 3. ECS services (scale to 0, delete) + deregister task definitions
###############################################################################
echo "=== 3/11 Deleting ECS services and task definitions ==="

CLUSTER="svc-experiment-cluster"

ECS_SERVICES=$(aws ecs list-services \
  --cluster "$CLUSTER" \
  --region "$REGION" \
  --query 'serviceArns' --output text 2>/dev/null) || true

for SVC_ARN in $ECS_SERVICES; do
  SVC_NAME=$(echo "$SVC_ARN" | awk -F'/' '{print $NF}')
  echo "  Scaling down and deleting ECS service: $SVC_NAME"
  aws ecs update-service --cluster "$CLUSTER" --service "$SVC_NAME" \
    --desired-count 0 --region "$REGION" || true
  aws ecs delete-service --cluster "$CLUSTER" --service "$SVC_NAME" \
    --force --region "$REGION" || true
done

# Deregister task definitions with svc- prefix
TASK_DEF_FAMILIES=$(aws ecs list-task-definition-families \
  --family-prefix svc- \
  --status ACTIVE \
  --region "$REGION" \
  --query 'families' --output text) || true

for FAMILY in $TASK_DEF_FAMILIES; do
  TASK_DEFS=$(aws ecs list-task-definitions \
    --family-prefix "$FAMILY" \
    --status ACTIVE \
    --region "$REGION" \
    --query 'taskDefinitionArns' --output text) || true
  for TD_ARN in $TASK_DEFS; do
    echo "  Deregistering task definition: $TD_ARN"
    aws ecs deregister-task-definition --task-definition "$TD_ARN" --region "$REGION" || true
  done
done

# Delete the ECS cluster
echo "  Deleting ECS cluster: $CLUSTER"
aws ecs delete-cluster --cluster "$CLUSTER" --region "$REGION" || true
echo "  ECS cleanup complete."

###############################################################################
# 4. ALB target groups (svc- prefix) + delete ALB
###############################################################################
echo "=== 4/11 Deleting ALB target groups and ALB ==="

# Delete listener rules first (non-default rules)
if [[ -n "${LISTENER_ARN:-}" ]]; then
  RULE_ARNS=$(aws elbv2 describe-rules \
    --listener-arn "$LISTENER_ARN" \
    --region "$REGION" \
    --query 'Rules[?!IsDefault].RuleArn' --output text) || true
  for RULE_ARN in $RULE_ARNS; do
    echo "  Deleting listener rule: $RULE_ARN"
    aws elbv2 delete-rule --rule-arn "$RULE_ARN" --region "$REGION" || true
  done
fi

# Delete target groups with svc- prefix
TG_ARNS=$(aws elbv2 describe-target-groups \
  --region "$REGION" \
  --query 'TargetGroups[?starts_with(TargetGroupName, `svc-`)].TargetGroupArn' \
  --output text) || true

for TG_ARN in $TG_ARNS; do
  echo "  Deleting target group: $TG_ARN"
  aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$REGION" || true
done

# Delete the ALB
if [[ -n "${ALB_ARN:-}" ]]; then
  echo "  Deleting ALB: $ALB_ARN"
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" --region "$REGION" || true
  echo "  Waiting for ALB to be fully deleted..."
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" --region "$REGION" || true
fi
echo "  ALB cleanup complete."

###############################################################################
# 5. DocumentDB cluster + instances + subnet group
###############################################################################
echo "=== 5/11 Deleting DocumentDB cluster and subnet group ==="

# Delete DocumentDB instance first
echo "  Deleting DocumentDB instance: svc-experiment-docdb-instance-1"
aws docdb delete-db-instance \
  --db-instance-identifier svc-experiment-docdb-instance-1 \
  --region "$REGION" || true

echo "  Waiting for DocumentDB instance deletion..."
aws docdb wait db-instance-deleted \
  --db-instance-identifier svc-experiment-docdb-instance-1 \
  --region "$REGION" || true

# Delete DocumentDB cluster (skip final snapshot)
echo "  Deleting DocumentDB cluster: svc-experiment-docdb"
aws docdb delete-db-cluster \
  --db-cluster-identifier svc-experiment-docdb \
  --skip-final-snapshot \
  --region "$REGION" || true

# Wait for cluster deletion before removing subnet group
echo "  Waiting for DocumentDB cluster deletion..."
sleep 30

# Retry loop — cluster deletion can take a while
for i in $(seq 1 20); do
  STATUS=$(aws docdb describe-db-clusters \
    --db-cluster-identifier svc-experiment-docdb \
    --region "$REGION" \
    --query 'DBClusters[0].Status' --output text 2>/dev/null) || STATUS="deleted"
  if [[ "$STATUS" == "deleted" ]]; then
    break
  fi
  echo "    Cluster status: $STATUS — waiting 30s (attempt $i/20)..."
  sleep 30
done

echo "  Deleting DocumentDB subnet group: svc-experiment-docdb-subnet-group"
aws docdb delete-db-subnet-group \
  --db-subnet-group-name svc-experiment-docdb-subnet-group \
  --region "$REGION" || true
echo "  DocumentDB cleanup complete."

###############################################################################
# 6. ECR repositories (svc-experiment/ with --force)
###############################################################################
echo "=== 6/11 Deleting ECR repositories ==="

for ARCHETYPE in event-driven-api batch-transform ml-inference enterprise-microservice; do
  echo "  Deleting ECR repo: svc-experiment/${ARCHETYPE}"
  aws ecr delete-repository \
    --repository-name "svc-experiment/${ARCHETYPE}" \
    --force \
    --region "$REGION" || true
done
echo "  ECR cleanup complete."

###############################################################################
# 7. S3 bucket (empty + delete)
###############################################################################
echo "=== 7/11 Deleting S3 bucket ==="

BUCKET_NAME="${BUCKET_NAME:-svc-experiment-data-${ACCOUNT_ID}}"

echo "  Emptying bucket: $BUCKET_NAME"
aws s3 rm "s3://${BUCKET_NAME}" --recursive --region "$REGION" || true

echo "  Deleting bucket: $BUCKET_NAME"
aws s3api delete-bucket --bucket "$BUCKET_NAME" --region "$REGION" || true
echo "  S3 cleanup complete."

###############################################################################
# 8. NAT Gateway + release EIP + detach/delete IGW
###############################################################################
echo "=== 8/11 Deleting NAT Gateway, EIP, and IGW ==="

# Delete NAT Gateway
if [[ -n "${NAT_GW:-}" ]]; then
  echo "  Deleting NAT Gateway: $NAT_GW"
  aws ec2 delete-nat-gateway --nat-gateway-id "$NAT_GW" --region "$REGION" || true

  # Wait for NAT GW to fully delete before releasing EIP
  echo "  Waiting for NAT Gateway deletion..."
  for i in $(seq 1 30); do
    NAT_STATE=$(aws ec2 describe-nat-gateways \
      --nat-gateway-ids "$NAT_GW" \
      --region "$REGION" \
      --query 'NatGateways[0].State' --output text 2>/dev/null) || NAT_STATE="deleted"
    if [[ "$NAT_STATE" == "deleted" ]]; then
      echo "  NAT Gateway deleted."
      break
    fi
    echo "    NAT GW state: $NAT_STATE — waiting 15s (attempt $i/30)..."
    sleep 15
  done
fi

# Release Elastic IP
if [[ -n "${EIP_ALLOC:-}" ]]; then
  echo "  Releasing Elastic IP: $EIP_ALLOC"
  aws ec2 release-address --allocation-id "$EIP_ALLOC" --region "$REGION" || true
fi

# Detach and delete Internet Gateway
if [[ -n "${IGW_ID:-}" && -n "${VPC_ID:-}" ]]; then
  echo "  Detaching IGW $IGW_ID from VPC $VPC_ID"
  aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$REGION" || true
  echo "  Deleting IGW: $IGW_ID"
  aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID" --region "$REGION" || true
fi
echo "  NAT GW / EIP / IGW cleanup complete."

###############################################################################
# 9. Subnets + route tables + security group + VPC
###############################################################################
echo "=== 9/11 Deleting subnets, route tables, security group, and VPC ==="

# Disassociate and delete route tables (non-main)
for RT in ${PUBLIC_RT:-} ${PRIVATE_RT:-}; do
  if [[ -n "$RT" ]]; then
    # Disassociate all explicit associations
    ASSOC_IDS=$(aws ec2 describe-route-tables \
      --route-table-ids "$RT" \
      --region "$REGION" \
      --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' \
      --output text 2>/dev/null) || true
    for ASSOC in $ASSOC_IDS; do
      echo "  Disassociating route table association: $ASSOC"
      aws ec2 disassociate-route-table --association-id "$ASSOC" --region "$REGION" || true
    done
    echo "  Deleting route table: $RT"
    aws ec2 delete-route-table --route-table-id "$RT" --region "$REGION" || true
  fi
done

# Delete subnets
for SUBNET in ${SUBNET_PUB_A:-} ${SUBNET_PRIV_A:-} ${SUBNET_PRIV_B:-}; do
  if [[ -n "$SUBNET" ]]; then
    echo "  Deleting subnet: $SUBNET"
    aws ec2 delete-subnet --subnet-id "$SUBNET" --region "$REGION" || true
  fi
done

# Delete security group
if [[ -n "${SG_ID:-}" ]]; then
  echo "  Deleting security group: $SG_ID"
  aws ec2 delete-security-group --group-id "$SG_ID" --region "$REGION" || true
fi

# Delete VPC
if [[ -n "${VPC_ID:-}" ]]; then
  echo "  Deleting VPC: $VPC_ID"
  aws ec2 delete-vpc --vpc-id "$VPC_ID" --region "$REGION" || true
fi
echo "  VPC / networking cleanup complete."

###############################################################################
# 10. IAM roles (detach all policies + delete)
###############################################################################
echo "=== 10/11 Deleting IAM roles ==="

for ROLE_NAME in svc-lambda-execution-role svc-fargate-execution-role svc-ec2-loadgen-role; do
  echo "  Processing IAM role: $ROLE_NAME"

  # Detach all managed policies
  ATTACHED_POLICIES=$(aws iam list-attached-role-policies \
    --role-name "$ROLE_NAME" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null) || true
  for POLICY_ARN in $ATTACHED_POLICIES; do
    echo "    Detaching policy: $POLICY_ARN"
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" || true
  done

  # Delete all inline policies
  INLINE_POLICIES=$(aws iam list-role-policies \
    --role-name "$ROLE_NAME" \
    --query 'PolicyNames' --output text 2>/dev/null) || true
  for POLICY_NAME in $INLINE_POLICIES; do
    echo "    Deleting inline policy: $POLICY_NAME"
    aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" || true
  done

  echo "  Deleting role: $ROLE_NAME"
  aws iam delete-role --role-name "$ROLE_NAME" || true
done
echo "  IAM cleanup complete."

###############################################################################
# 11. EC2 instance + instance profile
###############################################################################
echo "=== 11/11 Deleting EC2 instance and instance profile ==="

# Terminate EC2 instance
if [[ -n "${EC2_INSTANCE_ID:-}" ]]; then
  echo "  Terminating EC2 instance: $EC2_INSTANCE_ID"
  aws ec2 terminate-instances --instance-ids "$EC2_INSTANCE_ID" --region "$REGION" || true
  echo "  Waiting for instance termination..."
  aws ec2 wait instance-terminated --instance-ids "$EC2_INSTANCE_ID" --region "$REGION" || true
else
  # Try to find the instance by tag
  EC2_INSTANCE_ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=svc-experiment-loadgen" "Name=instance-state-name,Values=running,stopped" \
    --region "$REGION" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null) || true
  if [[ -n "$EC2_INSTANCE_ID" && "$EC2_INSTANCE_ID" != "None" ]]; then
    echo "  Terminating EC2 instance (found by tag): $EC2_INSTANCE_ID"
    aws ec2 terminate-instances --instance-ids "$EC2_INSTANCE_ID" --region "$REGION" || true
    echo "  Waiting for instance termination..."
    aws ec2 wait instance-terminated --instance-ids "$EC2_INSTANCE_ID" --region "$REGION" || true
  fi
fi

# Remove role from instance profile, then delete the profile
echo "  Removing role from instance profile: svc-ec2-loadgen-profile"
aws iam remove-role-from-instance-profile \
  --instance-profile-name svc-ec2-loadgen-profile \
  --role-name svc-ec2-loadgen-role || true

echo "  Deleting instance profile: svc-ec2-loadgen-profile"
aws iam delete-instance-profile \
  --instance-profile-name svc-ec2-loadgen-profile || true
echo "  EC2 cleanup complete."

###############################################################################
# Done
###############################################################################
echo ""
echo "============================================================"
echo "  Cleanup complete. All experiment resources have been"
echo "  deleted (or were already absent)."
echo "============================================================"
