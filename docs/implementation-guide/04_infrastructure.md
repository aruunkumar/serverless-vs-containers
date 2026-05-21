
# Experiment Implementation Guide — File 3 of 8
# Infrastructure Setup and Container Images

> **Cross-reference**: Read Files 1–2 before this file. All bash commands append resource IDs to `experiment-env.sh` — source this file at the start of every session.

---

## 4. Infrastructure Setup

### 4.1 Prerequisites

Ensure the following are in place before running any commands:

| Requirement | Details |
|---|---|
| **AWS Account** | With permissions for: Lambda, ECS, ECR, IAM, S3, API Gateway, ALB, CloudWatch, X-Ray, VPC, Cost Explorer |
| **AWS CLI v2** | Configured with credentials: run `aws configure` with your Access Key ID, Secret Access Key, region=us-east-1 |
| **Docker** | Docker Desktop or Docker Engine installed and running locally (for building container images) |
| **Python 3.11** | Python 3.11 installed locally and on the EC2 load generator instance |
| **Git** | Git installed for cloning SeBS and DeathStarBench repositories |
| **EC2 Instance** | c5.2xlarge in us-east-1 for running the load generator. This instance will run 7 days continuously. |
| **AWS Region** | ALL resources must be in us-east-1. Set default: `aws configure set region us-east-1` |

---

### 4.2 VPC and Network Setup

All Lambda functions and Fargate services must be in the same VPC for fair comparison. The VPC uses **public subnets** for the ALB and **private subnets** for Lambda/Fargate.

> **WHY NAT GATEWAY**: Private subnets have no direct internet access (for security). The NAT Gateway in the public subnet allows Lambda and Fargate tasks to make outbound connections to ECR (for image pulls), S3 (for payload data), CloudWatch, and X-Ray — without exposing them to inbound internet traffic. Cost: approximately $0.045/hr + data charges (~$22 for a 2-week experiment).

```bash
# Step 1: Create VPC
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=sebs-experiment-vpc}]' \
  --query 'Vpc.VpcId' --output text)

# Step 2: Create public subnets (for ALB) in two AZs
SUBNET_PUB_A=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.0.1.0/24 --availability-zone us-east-1a \
  --query 'Subnet.SubnetId' --output text)

SUBNET_PUB_B=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.0.2.0/24 --availability-zone us-east-1b \
  --query 'Subnet.SubnetId' --output text)

# Step 3: Create private subnets (for Lambda + Fargate)
SUBNET_PRIV_A=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.0.3.0/24 --availability-zone us-east-1a \
  --query 'Subnet.SubnetId' --output text)

SUBNET_PRIV_B=$(aws ec2 create-subnet --vpc-id $VPC_ID \
  --cidr-block 10.0.4.0/24 --availability-zone us-east-1b \
  --query 'Subnet.SubnetId' --output text)

# Step 4: Internet Gateway (for public subnets / ALB)
IGW_ID=$(aws ec2 create-internet-gateway --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID

# Step 5: NAT Gateway (for private subnets -> ECR/S3/CloudWatch/X-Ray)
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
NAT_GW=$(aws ec2 create-nat-gateway --subnet-id $SUBNET_PUB_A \
  --allocation-id $EIP_ALLOC --query 'NatGateway.NatGatewayId' --output text)
aws ec2 wait nat-gateway-available --nat-gateway-ids $NAT_GW

# Step 6: Public route table (IGW)
PUBLIC_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PUBLIC_RT --destination-cidr-block [IP_ADDRESS] --gateway-id $IGW_ID
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $SUBNET_PUB_A
aws ec2 associate-route-table --route-table-id $PUBLIC_RT --subnet-id $SUBNET_PUB_B

# Step 7: Private route table (NAT GW)
PRIVATE_RT=$(aws ec2 create-route-table --vpc-id $VPC_ID --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $PRIVATE_RT --destination-cidr-block [IP_ADDRESS] --nat-gateway-id $NAT_GW
aws ec2 associate-route-table --route-table-id $PRIVATE_RT --subnet-id $SUBNET_PRIV_A
aws ec2 associate-route-table --route-table-id $PRIVATE_RT --subnet-id $SUBNET_PRIV_B

# Step 8: Security Group (allow HTTP inbound + internal traffic)
SG_ID=$(aws ec2 create-security-group --group-name sebs-experiment-sg \
  --description "Serverless vs Container Experiment" --vpc-id $VPC_ID \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 80 --cidr [IP_ADDRESS]
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 8080 --cidr [IP_ADDRESS]
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol -1 --source-group $SG_ID

# Save all IDs for later use
echo "VPC_ID=$VPC_ID" >> experiment-env.sh
echo "SUBNET_PUB_A=$SUBNET_PUB_A" >> experiment-env.sh
echo "SUBNET_PUB_B=$SUBNET_PUB_B" >> experiment-env.sh
echo "SUBNET_PRIV_A=$SUBNET_PRIV_A" >> experiment-env.sh
echo "SUBNET_PRIV_B=$SUBNET_PRIV_B" >> experiment-env.sh
echo "SG_ID=$SG_ID" >> experiment-env.sh
echo "IGW_ID=$IGW_ID" >> experiment-env.sh
echo "NAT_GW=$NAT_GW" >> experiment-env.sh
echo "EIP_ALLOC=$EIP_ALLOC" >> experiment-env.sh
echo "PUBLIC_RT=$PUBLIC_RT" >> experiment-env.sh
echo "PRIVATE_RT=$PRIVATE_RT" >> experiment-env.sh
```

---

### 4.3 IAM Roles

#### 4.3.1 Lambda Execution Role

```bash
cat > lambda-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"}]
}
EOF

aws iam create-role --role-name sebs-lambda-execution-role \
  --assume-role-policy-document file://lambda-trust-policy.json

# Attach required policies
aws iam attach-role-policy --role-name sebs-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name sebs-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
aws iam attach-role-policy --role-name sebs-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name sebs-lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess

LAMBDA_ROLE_ARN=$(aws iam get-role --role-name sebs-lambda-execution-role \
  --query 'Role.Arn' --output text)
echo "LAMBDA_ROLE_ARN=$LAMBDA_ROLE_ARN" >> experiment-env.sh
```

#### 4.3.2 Fargate Task Execution Role

```bash
cat > ecs-trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow",
    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
    "Action": "sts:AssumeRole"}]
}
EOF

aws iam create-role --role-name sebs-fargate-execution-role \
  --assume-role-policy-document file://ecs-trust-policy.json

aws iam attach-role-policy --role-name sebs-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam attach-role-policy --role-name sebs-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name sebs-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess

FARGATE_ROLE_ARN=$(aws iam get-role --role-name sebs-fargate-execution-role \
  --query 'Role.Arn' --output text)
echo "FARGATE_ROLE_ARN=$FARGATE_ROLE_ARN" >> experiment-env.sh
```

---

### 4.4 S3 Bucket for Benchmark Data

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
BUCKET_NAME="sebs-experiment-data-${ACCOUNT_ID}"

aws s3 mb s3://${BUCKET_NAME} --region us-east-1

echo "BUCKET_NAME=$BUCKET_NAME" >> experiment-env.sh
echo "ACCOUNT_ID=$ACCOUNT_ID" >> experiment-env.sh
```

---

### 4.5 ECR Repositories (One Per Archetype)

```bash
source experiment-env.sh

for ARCHETYPE in thumbnailer etl-pipeline ml-inference hotel-reservation; do
  aws ecr create-repository \
    --repository-name sebs-experiment/${ARCHETYPE} \
    --region us-east-1
  echo "Created ECR repo: sebs-experiment/${ARCHETYPE}"
done

ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

# Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin ${ECR_REGISTRY}

echo "ECR_REGISTRY=$ECR_REGISTRY" >> experiment-env.sh
```

---

### 4.6 ECS Cluster

```bash
aws ecs create-cluster \
  --cluster-name sebs-experiment-cluster \
  --capacity-providers FARGATE \
  --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1
```

---

### 4.7 Application Load Balancer (for All Fargate Services)

```bash
source experiment-env.sh

# Create ALB in public subnets
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name sebs-experiment-alb \
  --subnets $SUBNET_PUB_A $SUBNET_PUB_B \
  --security-groups $SG_ID \
  --scheme internet-facing \
  --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns $ALB_ARN \
  --query 'LoadBalancers[0].DNSName' --output text)

# Create default listener (port 80)
LISTENER_ARN=$(aws elbv2 create-listener \
  --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=fixed-response,FixedResponseConfig='{"StatusCode":"404","ContentType":"text/plain","MessageBody":"Not Found"}' \
  --query 'Listeners[0].ListenerArn' --output text)

echo "ALB_ARN=$ALB_ARN" >> experiment-env.sh
echo "ALB_DNS=$ALB_DNS" >> experiment-env.sh
echo "LISTENER_ARN=$LISTENER_ARN" >> experiment-env.sh
echo "ALB endpoint: http://$ALB_DNS"
```
