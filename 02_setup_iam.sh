#!/bin/bash
set -euo pipefail

###############################################################################
# 02_setup_iam.sh
#
# Creates IAM roles for the serverless-vs-container benchmark experiment:
#   - Lambda execution role: svc-lambda-execution-role
#       Trust: lambda.amazonaws.com
#       Policies: AWSLambdaBasicExecutionRole, AWSLambdaVPCAccessExecutionRole,
#                 AmazonS3FullAccess
#   - Fargate task execution role: svc-fargate-execution-role
#       Trust: ecs-tasks.amazonaws.com
#       Policies: AmazonECSTaskExecutionRolePolicy, AmazonS3FullAccess
#
# Both role ARNs are persisted to experiment-env.sh.
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

# Source the env file (must exist from 01_setup_vpc.sh)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run 01_setup_vpc.sh first."
  exit 1
fi
source "$ENV_FILE"

###############################################################################
# Lambda Execution Role
###############################################################################
echo "=== Creating Lambda Execution Role ==="

cat > /tmp/lambda-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name svc-lambda-execution-role \
  --assume-role-policy-document file:///tmp/lambda-trust-policy.json \
  --tags Key=Project,Value=svc-experiment

echo "Attaching policies to svc-lambda-execution-role..."
aws iam attach-role-policy --role-name svc-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name svc-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
aws iam attach-role-policy --role-name svc-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

LAMBDA_ROLE_ARN=$(aws iam get-role --role-name svc-lambda-execution-role \
  --query 'Role.Arn' --output text)
echo "LAMBDA_ROLE_ARN=$LAMBDA_ROLE_ARN"

###############################################################################
# Fargate Task Execution Role
###############################################################################
echo "=== Creating Fargate Task Execution Role ==="

cat > /tmp/ecs-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ecs-tasks.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name svc-fargate-execution-role \
  --assume-role-policy-document file:///tmp/ecs-trust-policy.json \
  --tags Key=Project,Value=svc-experiment

echo "Attaching policies to svc-fargate-execution-role..."
aws iam attach-role-policy --role-name svc-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam attach-role-policy --role-name svc-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess

FARGATE_ROLE_ARN=$(aws iam get-role --role-name svc-fargate-execution-role \
  --query 'Role.Arn' --output text)
echo "FARGATE_ROLE_ARN=$FARGATE_ROLE_ARN"

###############################################################################
# Clean up temp files
###############################################################################
rm -f /tmp/lambda-trust-policy.json /tmp/ecs-trust-policy.json

###############################################################################
# Persist ARNs
###############################################################################
echo "=== Persisting role ARNs to $ENV_FILE ==="
{
  echo "LAMBDA_ROLE_ARN=$LAMBDA_ROLE_ARN"
  echo "FARGATE_ROLE_ARN=$FARGATE_ROLE_ARN"
} >> "$ENV_FILE"

echo "=== IAM setup complete ==="
echo "All role ARNs saved to $ENV_FILE"
