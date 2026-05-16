#!/bin/bash
set -euo pipefail

###############################################################################
# 01_setup_vpc.sh
#
# Creates the VPC and networking infrastructure for the serverless-vs-container
# benchmark experiment in us-east-2:
#   - VPC 10.0.0.0/16
#   - 1 public subnet  (10.0.1.0/24, us-east-2a) — NAT Gateway only
#   - 2 private subnets (10.0.2.0/24 us-east-2a, 10.0.3.0/24 us-east-2b)
#   - Internet Gateway attached to VPC
#   - NAT Gateway with Elastic IP in the public subnet
#   - Route tables: public (IGW), private (NAT GW)
#   - Security group svc-experiment-sg
#
# All resource IDs are persisted to experiment-env.sh.
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"

# Initialise the env file if it doesn't exist, then source it
touch "$ENV_FILE"
source "$ENV_FILE"

echo "=== Creating VPC ==="
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block 10.0.0.0/16 \
  --region "$REGION" \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=svc-experiment-vpc}]' \
  --query 'Vpc.VpcId' --output text)
echo "VPC_ID=$VPC_ID"

# Enable DNS hostnames (required for VPC endpoints / service discovery)
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}' --region "$REGION"
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}' --region "$REGION"

echo "=== Creating Subnets ==="

# Public subnet — hosts NAT Gateway only (no other resources)
SUBNET_PUB_A=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block 10.0.1.0/24 \
  --availability-zone "${REGION}a" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=svc-experiment-public-a}]' \
  --query 'Subnet.SubnetId' --output text)
echo "SUBNET_PUB_A=$SUBNET_PUB_A"

# Private subnet A — Lambda, Fargate, ALB, DocumentDB, EC2 load generator
SUBNET_PRIV_A=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block 10.0.2.0/24 \
  --availability-zone "${REGION}a" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=svc-experiment-private-a}]' \
  --query 'Subnet.SubnetId' --output text)
echo "SUBNET_PRIV_A=$SUBNET_PRIV_A"

# Private subnet B — second AZ for ALB / DocumentDB multi-AZ
SUBNET_PRIV_B=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block 10.0.3.0/24 \
  --availability-zone "${REGION}b" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=svc-experiment-private-b}]' \
  --query 'Subnet.SubnetId' --output text)
echo "SUBNET_PRIV_B=$SUBNET_PRIV_B"

echo "=== Creating Internet Gateway ==="
IGW_ID=$(aws ec2 create-internet-gateway \
  --region "$REGION" \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=svc-experiment-igw}]' \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$REGION"
echo "IGW_ID=$IGW_ID"

echo "=== Allocating Elastic IP and Creating NAT Gateway ==="
EIP_ALLOC=$(aws ec2 allocate-address \
  --domain vpc \
  --region "$REGION" \
  --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=svc-experiment-eip}]' \
  --query 'AllocationId' --output text)
echo "EIP_ALLOC=$EIP_ALLOC"

NAT_GW=$(aws ec2 create-nat-gateway \
  --subnet-id "$SUBNET_PUB_A" \
  --allocation-id "$EIP_ALLOC" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=natgateway,Tags=[{Key=Name,Value=svc-experiment-nat}]' \
  --query 'NatGateway.NatGatewayId' --output text)
echo "NAT_GW=$NAT_GW — waiting for it to become available..."
aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_GW" --region "$REGION"
echo "NAT Gateway is available."

echo "=== Creating Route Tables ==="

# Public route table: 0.0.0.0/0 → IGW
PUBLIC_RT=$(aws ec2 create-route-table \
  --vpc-id "$VPC_ID" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=svc-experiment-public-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PUBLIC_RT" --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$IGW_ID" --region "$REGION"
aws ec2 associate-route-table --route-table-id "$PUBLIC_RT" --subnet-id "$SUBNET_PUB_A" --region "$REGION"
echo "PUBLIC_RT=$PUBLIC_RT"

# Private route table: 0.0.0.0/0 → NAT GW
PRIVATE_RT=$(aws ec2 create-route-table \
  --vpc-id "$VPC_ID" \
  --region "$REGION" \
  --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=svc-experiment-private-rt}]' \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PRIVATE_RT" --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id "$NAT_GW" --region "$REGION"
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$SUBNET_PRIV_A" --region "$REGION"
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$SUBNET_PRIV_B" --region "$REGION"
echo "PRIVATE_RT=$PRIVATE_RT"

echo "=== Creating Security Group ==="
SG_ID=$(aws ec2 create-security-group \
  --group-name svc-experiment-sg \
  --description "Serverless vs Container Experiment SG" \
  --vpc-id "$VPC_ID" \
  --region "$REGION" \
  --query 'GroupId' --output text)

# Allow HTTP inbound on port 80 (from within VPC only — no public access)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 80 --cidr 10.0.0.0/16 --region "$REGION"

# Allow HTTP inbound on port 8080 (from within VPC only)
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 8080 --cidr 10.0.0.0/16 --region "$REGION"

# Allow all internal traffic within the security group
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol -1 --source-group "$SG_ID" --region "$REGION"
echo "SG_ID=$SG_ID"

echo "=== Persisting resource IDs to $ENV_FILE ==="
{
  echo "VPC_ID=$VPC_ID"
  echo "SUBNET_PUB_A=$SUBNET_PUB_A"
  echo "SUBNET_PRIV_A=$SUBNET_PRIV_A"
  echo "SUBNET_PRIV_B=$SUBNET_PRIV_B"
  echo "IGW_ID=$IGW_ID"
  echo "NAT_GW=$NAT_GW"
  echo "EIP_ALLOC=$EIP_ALLOC"
  echo "PUBLIC_RT=$PUBLIC_RT"
  echo "PRIVATE_RT=$PRIVATE_RT"
  echo "SG_ID=$SG_ID"
} >> "$ENV_FILE"

echo "=== VPC setup complete ==="
echo "All resource IDs saved to $ENV_FILE"
