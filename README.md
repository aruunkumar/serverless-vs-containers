# Serverless vs. Container Workload Placement — Empirical Benchmark

An empirical benchmark platform comparing **Serverless/ FaaS(AWS Lambda)** against **Containers / CaaS(AWS ECS Fargate)** under controlled, identical workloads. The platform deploys 32 parallel endpoints — 16 Lambda functions and 16 Fargate services running the *same* container images — drives them through 36 sequential load blocks each (4 frequencies × 3 burstiness levels × 3 duration tiers), and produces a 1,150-record dataset used to train regression models that predict cost, p95 latency, and cold-start rate from workload characteristics.
The work targets the IEEE Cloud Computing Conference.

## What's in here
```
serverless-container-experiments/
├── README.md                    
├── archetypes/                  Workload application code (4 archetypes)
│   ├── batch-transform/         pandas/pyarrow ETL
│   ├── enterprise-microservice/ Hotel reservation (Go + Python wrapper, MongoDB-on-DocumentDB)
│   ├── event-driven-api/        Image thumbnailer (Pillow)
│   └── ml-inference/            ResNet-50 classifier (PyTorch)
│
├── infrastructure/              Deployment scripts
│   ├── 01_setup_vpc.sh ... 06_setup_ec2.sh   Provisioning, in numbered order
│   ├── deploy_lambda.sh         Deploy 16 Lambda functions + API Gateways
│   ├── deploy_fargate.sh        Deploy 16 Fargate services + ALB rules
│   ├── build_and_push_images.sh Build all 16 OCI images, push to ECR
│   ├── monitor_experiment.sh    Live status during the 63-hour run
│   ├── cleanup.sh               Tear everything down
│   └── global-bundle.pem        DocumentDB CA bundle (shipped with containers)
│
├── load-generation/             Load generator + supporting tools
│   ├── load_generator.py        Main 32-thread orchestrator (runs on EC2)
│   ├── smoke_test.py            5-min/block smoke run for validation
│   ├── generate_payloads.py     Builds + uploads test payloads to S3
│   ├── generate_deployments_json.py  Parses endpoints into deployments.json
│   ├── validate_endpoints.py    Confirms all 32 endpoints respond
├── data-collection/             Post-experiment data gathering
│   ├── query_cold_starts.py     CloudWatch Logs Insights for Lambda cold starts
│   ├── query_latency.py         CloudWatch Metrics for Lambda + ALB latencies
│   ├── query_costs.py           Cost Explorer per-resource cost extraction
│   ├── query_alb_lcu.py         ALB ConsumedLCUs at 1-min resolution
│   ├── aggregate_results.py     Joins load-block CSVs with collected metrics
│   └── prepare_training_data.py Splits aggregated results into train/test
│
├── data-prep/
│   └── prepare_data.py          End-to-end curated-dataset producer used by training
│
├── data/                        All CSV/JSON data, separated by lifecycle stage
│   ├── raw/
│   │   ├── load-blocks/         32 CSVs from the full 63-hour run
│   │   └── costs.csv            Cost Explorer extract
│   ├── smoke/
│   │   └── load-blocks/         32 CSVs from smoke runs
│   └── processed/
│       ├── train.csv            Training split (gitignored)
│       ├── test.csv             Held-out split (gitignored)
│       ├── cold_starts.csv      Aligned cold-start metrics per block
│       └── alb_lcu_per_block.csv ALB consumption per block
│
├── training/                    ML pipeline (no outputs — outputs go to training-output/)
│   ├── train_models.py          Trains Ridge + RF for 4 targets, writes joblibs + schema
│   ├── predict.py               CLI to score new workload configurations
│   └── requirements.txt
│
├── training-output/
│   ├── run-1/                   Baseline (no cold starts, no ALB LCU)
│   └── run-2/                   Final run with all signals
│       ├── models/              Serialized estimators + feature_schema.json
│       ├── model_summary.json   R², RMSE, CIs, feature importances
│       └── *.png                Predicted-vs-actual plots, feature importance
│
├── docs/
│   ├── implementation-guide/        Step-by-step build/deploy guides (8 numbered docs)

```

## Quick start
```bash
# 0. Local environment for analysis only (the load generator runs on EC2)

python3 -m venv .venv && .venv/bin/pip install -r training/requirements.txt

# 1. Provision AWS infrastructure (≈ 30 minutes)

bash infrastructure/01_setup_vpc.sh        # VPC, subnets, security groups

bash infrastructure/02_setup_iam.sh        # Lambda + Fargate execution roles

bash infrastructure/03_setup_storage.sh    # S3 + ECR

bash infrastructure/04_setup_ecs_alb.sh    # ECS cluster + internal ALB

bash infrastructure/05_setup_docdb.sh      # DocumentDB for the stateful archetype

# 2. Build and push images, deploy endpoints

python3 load-generation/generate_payloads.py

bash infrastructure/build_and_push_images.sh

bash infrastructure/deploy_lambda.sh

bash infrastructure/deploy_fargate.sh

python3 load-generation/generate_deployments_json.py

# 3. Stand up the EC2 load generator

bash infrastructure/06_setup_ec2.sh

# Connect via SSM. On the EC2 box:

#    python3.11 validate_endpoints.py

#    python3.11 smoke_test.py --config deployments.json --output data/smoke/load-blocks/

#    python3.11 load_generator.py --config deployments.json --output data/raw/load-blocks/

# 4. Pull data back, train models

python3 data-collection/query_cold_starts.py --start-time ... --end-time ... --output data/processed/cold_starts.csv

python3 data-collection/query_alb_lcu.py    --start-time ... --end-time ... --output data/processed/alb_lcu_per_block.csv

python3 data-prep/prepare_data.py            --load-dir data/raw/load-blocks/ --output-dir data/processed/

.venv/bin/python training/train_models.py    --input-dir data/processed/     --output-dir training-output/run-N/

# 5. Use the trained models on an arbitrary workload

.venv/bin/python training/predict.py --models-dir training-output/run-2/models \
    --archetype event-driven-api --memory 512 --image-size slim \
    --frequency 50k --cv 2.0 --duration medium

# 6. Tear everything down

bash infrastructure/cleanup.sh

```

The full experiment takes ~63 hours wall-clock (32 deployments running in parallel, 36 blocks of 105 minutes each per deployment) and incurs $50–80/day in AWS costs while running. **Don't forget step 6.**


## Where to look first

- **What was measured and why** — [`docs/specs/requirements.md`](docs/specs/requirements.md) and [`docs/specs/design.md`](docs/specs/design.md)

- **Operating the platform** — [`docs/implementation-guide/`](docs/implementation-guide/) (8 numbered docs covering setup → cleanup) 

- **The trained models** — [`training-output/run-2/models/`](training-output/run-2/models/) (joblib artifacts + `feature_schema.json`); use [`training/predict.py`](training/predict.py) to score new inputs




