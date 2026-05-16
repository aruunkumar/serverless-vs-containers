#!/bin/bash
set -euo pipefail

###############################################################################
# 05_setup_docdb.sh
#
# Creates DocumentDB infrastructure for the Enterprise Microservice archetype
# in us-east-2:
#   - DocumentDB subnet group: svc-experiment-docdb-subnet-group
#     (using SUBNET_PRIV_A and SUBNET_PRIV_B)
#   - DocumentDB cluster: svc-experiment-docdb
#     (in private subnets, accessible from SG_ID)
#   - DocumentDB primary instance: svc-experiment-docdb-instance-1
#
# Waits for cluster and instance to become available, then persists
# DOCDB_ENDPOINT to experiment-env.sh.
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
# DocumentDB Subnet Group
###############################################################################
echo "=== Creating DocumentDB Subnet Group ==="

aws docdb create-db-subnet-group \
  --db-subnet-group-name svc-experiment-docdb-subnet-group \
  --db-subnet-group-description "Subnet group for svc-experiment DocumentDB cluster" \
  --subnet-ids "$SUBNET_PRIV_A" "$SUBNET_PRIV_B" \
  --region "$REGION"
echo "DocumentDB subnet group svc-experiment-docdb-subnet-group created."

###############################################################################
# DocumentDB Cluster
###############################################################################
echo "=== Creating DocumentDB Cluster ==="

aws docdb create-db-cluster \
  --db-cluster-identifier svc-experiment-docdb \
  --engine docdb \
  --master-username masteruser \
  --master-user-password ExperimentPass123 \
  --db-subnet-group-name svc-experiment-docdb-subnet-group \
  --vpc-security-group-ids "$SG_ID" \
  --no-deletion-protection \
  --region "$REGION"
echo "DocumentDB cluster svc-experiment-docdb created — waiting for it to become available..."

###############################################################################
# DocumentDB Primary Instance
###############################################################################
echo "=== Creating DocumentDB Primary Instance ==="

aws docdb create-db-instance \
  --db-cluster-identifier svc-experiment-docdb \
  --db-instance-identifier svc-experiment-docdb-instance-1 \
  --db-instance-class db.r5.large \
  --engine docdb \
  --region "$REGION"
echo "DocumentDB instance svc-experiment-docdb-instance-1 created — waiting for it to become available..."

aws docdb wait db-instance-available \
  --db-instance-identifier svc-experiment-docdb-instance-1 \
  --region "$REGION"
echo "DocumentDB instance is available."

###############################################################################
# Retrieve and persist cluster endpoint
###############################################################################
echo "=== Retrieving DocumentDB Cluster Endpoint ==="

DOCDB_ENDPOINT=$(aws docdb describe-db-clusters \
  --db-cluster-identifier svc-experiment-docdb \
  --region "$REGION" \
  --query 'DBClusters[0].Endpoint' --output text)
echo "DOCDB_ENDPOINT=$DOCDB_ENDPOINT"

###############################################################################
# Persist values
###############################################################################
echo "=== Persisting DocumentDB values to $ENV_FILE ==="
{
  echo "DOCDB_ENDPOINT=$DOCDB_ENDPOINT"
} >> "$ENV_FILE"

echo "=== DocumentDB setup complete ==="
echo "Cluster endpoint: $DOCDB_ENDPOINT"
echo "All values saved to $ENV_FILE"
