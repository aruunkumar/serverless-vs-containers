#!/bin/bash
set -euo pipefail

###############################################################################
# 06_setup_ec2.sh
#
# Provisions the EC2 load generator instance for the serverless-vs-container
# benchmark experiment in us-east-2:
#   - IAM role svc-ec2-loadgen-role with SSM, CloudWatch read, Cost Explorer read
#   - Instance profile for the role
#   - c5.2xlarge instance in private subnet (no public IP, no SSH key)
#   - User data: Python 3.11, numpy, requests, git, gcc, make, openssl-devel,
#     wrk2 built from source at /usr/local/bin/wrk2
#   - Copies load_generator.py, deployments.json, lua/ to the instance via SSM
#
# Access is via SSM Session Manager only.
# EC2_INSTANCE_ID is persisted to experiment-env.sh.
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

# Source the env file (must exist from prior setup scripts)
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run infrastructure setup scripts first."
  exit 1
fi
source "$ENV_FILE"

###############################################################################
# IAM Role and Instance Profile
###############################################################################
echo "=== Creating EC2 Load Generator IAM Role ==="

cat > /tmp/ec2-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ec2.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name svc-ec2-loadgen-role \
  --assume-role-policy-document file:///tmp/ec2-trust-policy.json \
  --tags Key=Project,Value=svc-experiment

echo "Attaching managed policies to svc-ec2-loadgen-role..."
aws iam attach-role-policy --role-name svc-ec2-loadgen-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name svc-ec2-loadgen-role \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess

echo "Creating inline policy for Cost Explorer read access..."
cat > /tmp/ce-read-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name svc-ec2-loadgen-role \
  --policy-name svc-cost-explorer-read \
  --policy-document file:///tmp/ce-read-policy.json

echo "Creating inline policy for API Gateway invoke access..."
cat > /tmp/apigw-invoke-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:us-east-2:*:*/*/*/*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name svc-ec2-loadgen-role \
  --policy-name svc-apigw-invoke \
  --policy-document file:///tmp/apigw-invoke-policy.json

echo "Creating instance profile and adding role..."
aws iam create-instance-profile \
  --instance-profile-name svc-ec2-loadgen-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name svc-ec2-loadgen-profile \
  --role-name svc-ec2-loadgen-role

# Allow time for instance profile to propagate
echo "Waiting 10s for instance profile propagation..."
sleep 10

###############################################################################
# Look up latest Amazon Linux 2023 AMI
###############################################################################
echo "=== Querying latest Amazon Linux 2023 AMI ==="

AMI_ID=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --region "$REGION" \
  --query 'Parameters[0].Value' --output text)
echo "AMI_ID=$AMI_ID"

###############################################################################
# User Data Script
###############################################################################
echo "=== Preparing user data script ==="

cat > /tmp/ec2-userdata.sh << 'USERDATA'
#!/bin/bash
set -euo pipefail
exec > /var/log/userdata.log 2>&1

echo "=== Installing system packages ==="
dnf update -y
dnf install -y python3.11 python3.11-pip git gcc make openssl-devel zlib-devel

echo "=== Installing Python packages ==="
python3.11 -m pip install numpy requests

echo "=== Building wrk2 from source ==="
cd /tmp
git clone https://github.com/giltene/wrk2.git
cd wrk2
make -j$(nproc)
cp wrk /usr/local/bin/wrk2
chmod +x /usr/local/bin/wrk2

echo "=== Verifying installations ==="
python3.11 --version
wrk2 --version || true

echo "=== Creating working directory ==="
mkdir -p /home/ssm-user/experiment/lua

echo "=== User data complete ==="
USERDATA

USERDATA_B64=$(base64 < /tmp/ec2-userdata.sh)

###############################################################################
# Launch EC2 Instance
###############################################################################
echo "=== Launching c5.2xlarge instance ==="

EC2_INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type c5.2xlarge \
  --subnet-id "$SUBNET_PRIV_A" \
  --security-group-ids "$SG_ID" \
  --iam-instance-profile Name=svc-ec2-loadgen-profile \
  --no-associate-public-ip-address \
  --user-data "$USERDATA_B64" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=svc-experiment-loadgen},{Key=Project,Value=svc-experiment}]' \
  --region "$REGION" \
  --query 'Instances[0].InstanceId' --output text)
echo "EC2_INSTANCE_ID=$EC2_INSTANCE_ID"

echo "Waiting for instance to reach running state..."
aws ec2 wait instance-running \
  --instance-ids "$EC2_INSTANCE_ID" \
  --region "$REGION"
echo "Instance is running."

###############################################################################
# Wait for SSM Agent to register the instance
###############################################################################
echo "=== Waiting for SSM agent to register instance ==="

MAX_ATTEMPTS=30
ATTEMPT=0
while [[ $ATTEMPT -lt $MAX_ATTEMPTS ]]; do
  SSM_STATUS=$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$EC2_INSTANCE_ID" \
    --region "$REGION" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo "None")

  if [[ "$SSM_STATUS" == "Online" ]]; then
    echo "SSM agent is online."
    break
  fi

  ATTEMPT=$((ATTEMPT + 1))
  echo "  Attempt $ATTEMPT/$MAX_ATTEMPTS — SSM status: $SSM_STATUS. Waiting 20s..."
  sleep 20
done

if [[ "$SSM_STATUS" != "Online" ]]; then
  echo "ERROR: SSM agent did not come online after $MAX_ATTEMPTS attempts."
  exit 1
fi

###############################################################################
# Wait for user data to complete
###############################################################################
echo "=== Waiting for user data script to finish ==="

MAX_ATTEMPTS=30
ATTEMPT=0
while [[ $ATTEMPT -lt $MAX_ATTEMPTS ]]; do
  CLOUD_INIT_STATUS=$(aws ssm send-command \
    --instance-ids "$EC2_INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["cloud-init status --format json 2>/dev/null | python3.11 -c \"import sys,json; print(json.load(sys.stdin).get(\\\"status\\\",\\\"unknown\\\"))\" 2>/dev/null || echo unknown"]' \
    --region "$REGION" \
    --query 'Command.CommandId' --output text)

  sleep 5

  RESULT=$(aws ssm get-command-invocation \
    --command-id "$CLOUD_INIT_STATUS" \
    --instance-id "$EC2_INSTANCE_ID" \
    --region "$REGION" \
    --query 'StandardOutputContent' --output text 2>/dev/null || echo "unknown")
  RESULT=$(echo "$RESULT" | tr -d '[:space:]')

  if [[ "$RESULT" == "done" ]]; then
    echo "User data script completed."
    break
  fi

  ATTEMPT=$((ATTEMPT + 1))
  echo "  Attempt $ATTEMPT/$MAX_ATTEMPTS — cloud-init status: $RESULT. Waiting 30s..."
  sleep 30
done

if [[ "$RESULT" != "done" ]]; then
  echo "WARNING: cloud-init may not have finished (status: $RESULT). Proceeding anyway."
fi

###############################################################################
# Copy load generator files to instance via S3
###############################################################################
echo "=== Copying load generator files to instance via S3 ==="

S3_STAGING="s3://${BUCKET_NAME}/staging/ec2-files"
REMOTE_DIR="/home/ssm-user/experiment"

# Upload files to S3 staging area
aws s3 cp scripts/load_generator.py "${S3_STAGING}/load_generator.py" --region "$REGION"
aws s3 cp scripts/validate_endpoints.py "${S3_STAGING}/validate_endpoints.py" --region "$REGION"
aws s3 cp scripts/smoke_test.py "${S3_STAGING}/smoke_test.py" --region "$REGION" 2>/dev/null || true
aws s3 cp scripts/generate_payloads.py "${S3_STAGING}/generate_payloads.py" --region "$REGION" 2>/dev/null || true

if [[ -f "deployments.json" ]]; then
  aws s3 cp deployments.json "${S3_STAGING}/deployments.json" --region "$REGION"
else
  echo "WARNING: deployments.json not found locally — generate it before running the experiment."
fi

if [[ -f "endpoints.txt" ]]; then
  aws s3 cp endpoints.txt "${S3_STAGING}/endpoints.txt" --region "$REGION"
fi

for LUA_FILE in lua/*.lua; do
  FILENAME=$(basename "$LUA_FILE")
  aws s3 cp "$LUA_FILE" "${S3_STAGING}/lua/${FILENAME}" --region "$REGION"
done

echo "Files uploaded to ${S3_STAGING}"

# Pull files down on the instance via SSM
CMD_ID=$(aws ssm send-command \
  --instance-ids "$EC2_INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters commands=["mkdir -p ${REMOTE_DIR}/lua && aws s3 cp ${S3_STAGING}/ ${REMOTE_DIR}/ --recursive --region ${REGION} && chown -R ssm-user:ssm-user ${REMOTE_DIR}"] \
  --region "$REGION" \
  --query 'Command.CommandId' --output text)

# Wait for download to complete
sleep 10
aws ssm wait command-executed \
  --command-id "$CMD_ID" \
  --instance-id "$EC2_INSTANCE_ID" \
  --region "$REGION" 2>/dev/null || true

DL_STATUS=$(aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$EC2_INSTANCE_ID" \
  --region "$REGION" \
  --query 'Status' --output text 2>/dev/null || echo "Unknown")

if [[ "$DL_STATUS" == "Success" ]]; then
  echo "Files downloaded to instance at ${REMOTE_DIR}"
else
  echo "WARNING: File download may have failed (status: $DL_STATUS)"
  echo "You can manually copy files: aws s3 cp ${S3_STAGING}/ ${REMOTE_DIR}/ --recursive"
fi

###############################################################################
# Clean up temp files
###############################################################################
rm -f /tmp/ec2-trust-policy.json /tmp/ce-read-policy.json /tmp/ec2-userdata.sh

###############################################################################
# Persist EC2 Instance ID
###############################################################################
echo "=== Persisting EC2 instance ID to $ENV_FILE ==="
echo "EC2_INSTANCE_ID=$EC2_INSTANCE_ID" >> "$ENV_FILE"

echo "=== EC2 load generator setup complete ==="
echo "Instance ID: $EC2_INSTANCE_ID"
echo "Access via: aws ssm start-session --target $EC2_INSTANCE_ID --region $REGION"
echo "All resource IDs saved to $ENV_FILE"
