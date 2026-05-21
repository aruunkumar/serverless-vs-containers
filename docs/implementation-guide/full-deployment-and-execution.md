Here's the full deployment and execution sequence. Everything runs in us-east-2.

## Prerequisites

- AWS CLI v2 configured with credentials that have admin-level access
- Podman, Finch, or Docker installed (podman is the default; override with `CONTAINER_RUNTIME=finch`)
- Python 3.11 with `boto3`, `pandas`, `numpy`, `scikit-learn`, `Pillow` installed locally
- Enough AWS service quotas for 16 Lambda functions, 16 Fargate tasks, a c5.2xlarge EC2, and a DocumentDB cluster
- If building on Apple Silicon (M1/M2/M3/M4), images are automatically built for `linux/amd64` via QEMU emulation

## Phase 1: Provision Infrastructure (run from project root)

```bash
# 1. VPC, subnets, NAT GW, security group
bash scripts/01_setup_vpc.sh

# 2. IAM roles for Lambda and Fargate
bash scripts/02_setup_iam.sh

# 3. S3 bucket + 4 ECR repos
bash scripts/03_setup_storage.sh

# 4. ECS cluster + internal ALB
bash scripts/04_setup_ecs_alb.sh

# 5. DocumentDB cluster
bash scripts/05_setup_docdb.sh
```

Each script appends resource IDs to `experiment-env.sh`. Wait for each to complete before running the next. DocumentDB can take 10-15 minutes.

## Phase 2: Build and Push Container Images

```bash
# Generate test payloads and upload to S3 (skip DocumentDB — not reachable from laptop)
python3 scripts/generate_payloads.py --skip-docdb

# Build all 16 container images and push to ECR
bash scripts/build_and_push_images.sh
```

The image build takes a while — especially ML inference with the ResNet-50 weights. On Apple Silicon, expect ~15 min per ML inference variant due to QEMU emulation.

## Phase 3: Deploy 32 Endpoints

```bash
# Deploy 16 Lambda functions + API Gateway HTTP APIs
bash scripts/deploy_lambda.sh

# Deploy 16 Fargate services + ALB target groups
bash scripts/deploy_fargate.sh
```

Both scripts write endpoint URLs to `endpoints.txt`.

## Phase 4: Prepare

```bash
# Generate deployments.json from endpoints.txt
python3 scripts/generate_deployments_json.py
```

All 32 endpoints require validation from inside the VPC — Lambda endpoints use IAM auth (SigV4 signing) and Fargate endpoints are behind an internal ALB. Full validation happens in Phase 5 from the EC2 instance.

## Phase 5: Set Up EC2 Load Generator

```bash
bash scripts/06_setup_ec2.sh
```

This provisions the c5.2xlarge, installs dependencies, builds wrk2, and copies scripts to the instance via S3 staging. Takes ~10 minutes.

After the EC2 instance is ready, copy any additional files and seed DocumentDB:

```bash
source experiment-env.sh

# Upload extra files to S3 staging
aws s3 cp scripts/validate_endpoints.py s3://$BUCKET_NAME/staging/ec2-files/ --region us-east-2
aws s3 cp scripts/smoke_test.py s3://$BUCKET_NAME/staging/ec2-files/ --region us-east-2
aws s3 cp scripts/generate_payloads.py s3://$BUCKET_NAME/staging/ec2-files/ --region us-east-2
aws s3 cp endpoints.txt s3://$BUCKET_NAME/staging/ec2-files/ --region us-east-2

# Connect to EC2 via SSM
aws ssm start-session --target $EC2_INSTANCE_ID --region us-east-2

# On the EC2 instance:
cd /home/ssm-user/experiment
aws s3 cp s3://<BUCKET_NAME>/staging/ec2-files/ . --recursive --region us-east-2

# Install Python dependencies
python3.11 -m pip install numpy requests boto3 botocore pymongo pandas

# Seed DocumentDB (only reachable from inside the VPC)
python3.11 generate_payloads.py --only-docdb

# Validate ALL 32 endpoints (both Lambda + Fargate)
python3.11 validate_endpoints.py
```

Don't proceed until validation shows 32/32 PASS. Fargate services may need a couple minutes to stabilize after deployment.

## Phase 5.5: Smoke Test (recommended before full experiment)

Run a compressed version of the experiment to verify everything works end-to-end. This runs 2 blocks per deployment (instead of 36) with 5-minute active load and 1-minute idle — finishes in ~12 minutes.

```bash
# On the EC2 instance:
cd /home/ssm-user/experiment
python3.11 smoke_test.py --config deployments.json --output smoke_results/
```

The smoke test:
- Runs all 32 deployments in parallel (same as the full experiment)
- Tests 2 parameter combinations per deployment: (10k freq, 0.5 CV, small tier) and (10k freq, 0.5 CV, medium tier)
- Uses the same Gamma distribution, SigV4 signing, and wrk2 integration as the real load generator
- Prints a pass/fail report at the end

If any deployments fail, fix them before starting the full 7-day run. Common issues:
- Fargate 404s → container needs redeployment (`bash scripts/deploy_fargate.sh`)
- Lambda 500s → check CloudWatch logs (`aws logs tail /aws/lambda/<function-name> --region us-east-2 --since 30m`)
- Timeouts → service may still be starting up, wait and retry

Results are written to `smoke_results/` — you can inspect the CSVs to verify latency and error rates look reasonable.

## Phase 6: Run the 7-Day Experiment

Once the smoke test passes, start the full experiment on the EC2 instance:

```bash
# Connect
aws ssm start-session --target $EC2_INSTANCE_ID --region us-east-2

# On the instance:
cd /home/ssm-user/experiment
nohup python3.11 load_generator.py \
  --config deployments.json \
  --output results/ \
  > /dev/null 2>&1 &
```

The experiment runs 36 blocks × 32 deployments = 1,152 blocks. Each block is 90 min active + 15 min idle, so the full run takes ~63 hours per deployment (all 32 run in parallel).

## Phase 7: Monitor (during the run)

From your local machine or the EC2 instance:

```bash
# Check process status
./scripts/monitor_experiment.sh status

# Count completed blocks
./scripts/monitor_experiment.sh blocks

# Check Lambda health
./scripts/monitor_experiment.sh lambda

# Check Fargate health
./scripts/monitor_experiment.sh fargate

# Recover a stopped Fargate service
./scripts/monitor_experiment.sh redeploy svc-event-driven-api-512mb-slim-container
```

## Phase 8: Collect Data (after experiment completes)

```bash
# Copy results from EC2 (via SSM)
# Then run data collection:

python3 scripts/query_cold_starts.py \
  --start-time 2026-04-25T00:00:00Z \
  --end-time 2026-05-02T00:00:00Z \
  --output cold_starts.csv

python3 scripts/query_latency.py \
  --start-time 2026-04-25T00:00:00Z \
  --end-time 2026-05-02T00:00:00Z \
  --alb-arn-suffix app/svc-experiment-alb/<your-alb-hash> \
  --output latency.csv

python3 scripts/query_costs.py \
  --start-date 2026-04-25 \
  --end-date 2026-05-02 \
  --output costs.csv
```

Adjust the dates to match your actual experiment window. The ALB ARN suffix is in `experiment-env.sh`.

## Phase 9: Aggregate and Prepare Training Data

```bash
python3 scripts/aggregate_results.py \
  --load-dir results/ \
  --cold-starts cold_starts.csv \
  --latency latency.csv \
  --costs costs.csv \
  --output results.csv

python3 scripts/prepare_training_data.py \
  --input results.csv \
  --output-dir data/
```

This produces `results.csv` (1,152 rows), `data/train.csv` (922 rows), and `data/test.csv` (230 rows).

## Phase 10: Cleanup

Back up `results.csv`, `train.csv`, and `test.csv` first, then:

```bash
bash scripts/cleanup.sh
```

The script will ask for confirmation before deleting everything. It tears down all 32 endpoints, DocumentDB, ECR repos, S3, networking, IAM roles, and the EC2 instance.

---

One heads-up: the biggest cost drivers are the DocumentDB cluster and the 16 always-on Fargate tasks. Budget roughly $50-80/day while the experiment is running. The cleanup script stops all charges.