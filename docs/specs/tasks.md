# Implementation Plan: Serverless Container Benchmark

## Overview

This plan implements the complete experiment platform for comparing AWS Lambda vs ECS Fargate. Tasks are ordered to build incrementally: infrastructure scripts → handlers → container images → deployment scripts → load generator → data collection → training data prep → cleanup. All code is Python 3.11 and bash (AWS CLI). No IaC.

## Tasks

- [x] 1. Implement infrastructure provisioning scripts
  - [x] 1.1 Create `scripts/01_setup_vpc.sh` — VPC (10.0.0.0/16), 1 public subnet (NAT GW only), 2 private subnets, IGW, NAT GW with EIP, route tables, security group `svc-experiment-sg`, persist all IDs to `experiment-env.sh`
    - Use `set -euo pipefail`, source and append to `experiment-env.sh`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
  - [x] 1.2 Create `scripts/02_setup_iam.sh` — Lambda execution role `svc-lambda-execution-role` with AWSLambdaBasicExecutionRole, AWSLambdaVPCAccessExecutionRole, AmazonS3FullAccess; Fargate execution role `svc-fargate-execution-role` with AmazonECSTaskExecutionRolePolicy, AmazonS3FullAccess; persist ARNs
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - [x] 1.3 Create `scripts/03_setup_storage.sh` — S3 bucket `svc-experiment-data-{ACCOUNT_ID}`, 4 ECR repos (`svc-experiment/event-driven-api`, `svc-experiment/batch-transform`, `svc-experiment/ml-inference`, `svc-experiment/enterprise-microservice`), persist bucket name and ECR registry URL
    - _Requirements: 3.1, 3.2, 3.3_
  - [x] 1.4 Create `scripts/04_setup_ecs_alb.sh` — ECS cluster `svc-experiment-cluster` with FARGATE capacity provider, internal ALB `svc-experiment-alb` in private subnets, default HTTP listener on port 80 with 404 fixed response, persist ALB ARN/DNS/listener ARN
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 1.5 Create `scripts/05_setup_docdb.sh` — DocumentDB subnet group using 2 private subnets, DocumentDB cluster in us-east-2, persist cluster endpoint as `DOCDB_ENDPOINT`
    - _Requirements: 5.1, 5.2, 5.3_
  - [ ]* 1.6 Write property test for experiment-env.sh round-trip persistence
    - **Property 1: Experiment-env.sh Round-Trip Persistence**
    - Test that arbitrary key-value pairs written in `KEY=VALUE` format can be parsed back identically
    - **Validates: Requirements 1.5, 2.5, 3.3, 4.4, 5.3**

- [x] 2. Checkpoint — Verify infrastructure scripts
  - Ensure all 5 infrastructure scripts are syntactically correct bash, use `set -euo pipefail`, and consistently append to `experiment-env.sh`. Ask the user if questions arise.

- [x] 3. Implement workload handler code
  - [x] 3.1 Update `archetypes/event-driven-api/handler.py` — adapt existing thumbnailer handler from `05_build_images.md` to use canonical archetype name in S3 paths (`payloads/event-driven-api/`), update bucket env var to `DATA_BUCKET`, ensure tiered processing (small: resize; medium: resize + format + EXIF; large: resize + multi-format + watermark)
    - Preserve SeBS 210.thumbnailer logic, wrap with Lambda `handler()` + Fargate Flask dual-entrypoint
    - _Requirements: 6.1, 6.5, 6.6, 6.7_
  - [x] 3.2 Create `archetypes/batch-transform/handler.py` — ETL pipeline handler: download CSV from S3, small=10K rows single-column aggregation, medium=100K rows multi-column + type casting + null handling, large=1M rows full pipeline with Parquet export to S3
    - Use pandas + pyarrow, dual-entrypoint pattern (Lambda handler + Flask on port 8080)
    - _Requirements: 6.2, 6.5, 6.6, 6.7_
  - [x] 3.3 Create `archetypes/ml-inference/handler.py` — ML inference handler: load pre-trained model once globally, small=batch 1 MobileNetV2, medium=batch 4 ResNet-50, large=batch 8 ResNet-50, use torchvision transforms (Resize 256 → CenterCrop 224 → ToTensor → Normalize)
    - Model baked into container image at `/app/models/`, dual-entrypoint pattern
    - Reference: `../serverless-benchmarks/benchmarks/400.inference/411.image-recognition/python/function.py`
    - _Requirements: 6.3, 6.5, 6.6, 6.7_
  - [x] 3.4 Create `archetypes/enterprise-microservice/handler.py` — Hotel reservation handler in Python: connect to DocumentDB via `DOCDB_ENDPOINT` env var, implement `GET /hotels` (geo-proximity search), `GET /recommendations` (search + scoring), `POST /reservation` (full booking with pymongo transaction), plus `POST /invoke` and `GET /health` for standard interface
    - Python rewrite of DeathStarBench Go hotel-reservation, using pymongo + Flask
    - Reference: `../DeathStarBench/hotelReservation/`
    - _Requirements: 6.4, 5.4, 6.5, 6.6, 6.7_
  - [ ]* 3.5 Write property test for handler interface invariant
    - **Property 3: Handler Interface Invariant**
    - For any valid archetype and tier payload, handler returns statusCode 200 with JSON body containing `payload_tier` and `execution_ms` (non-negative)
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
  - [x] 3.6 Create `requirements-slim.txt` and `requirements-standard.txt` for each archetype directory
    - Slim: core deps only (boto3, flask, + archetype-specific: Pillow / pandas+pyarrow / torch+torchvision / pymongo)
    - Standard: adds gunicorn, numpy, requests, pydantic, structlog, aws-lambda-powertools
    - _Requirements: 7.1_

- [x] 4. Implement container image build system
  - [x] 4.1 Create `Dockerfile.slim` and `Dockerfile.standard` for each archetype using multi-stage builds — Lambda targets use `public.ecr.aws/lambda/python:3.11`, Fargate targets use `python:3.11-slim` (slim) or `python:3.11` (standard)
    - ML Inference standard image must include ResNet-50 weights (~98MB) at `/app/models/`
    - Tag pattern: `serverless-slim`, `serverless-standard`, `container-slim`, `container-standard`
    - _Requirements: 7.1, 7.2, 7.4, 7.5_
  - [x] 4.2 Create `scripts/build_and_push_images.sh` — iterate all 4 archetypes, build and push all 4 variants per archetype (16 images total) to ECR repos at `svc-experiment/{archetype}:{tag}`
    - Source `experiment-env.sh` for ECR registry URL, authenticate Docker to ECR
    - _Requirements: 7.3_

- [x] 5. Implement payload generation and upload
  - [x] 5.1 Create `scripts/generate_payloads.py` — generate and upload all test payloads to S3: event-driven-api images (50KB JPEG, 500KB PNG, 2MB TIFF), batch-transform CSVs (10K/100K/1M rows with correct column schema and status distribution), ML inference test images, DocumentDB seed data (hotels, users, reservations)
    - ETL CSV columns: id, value_a (normal μ=100 σ=25), value_b (normal μ=50 σ=10), category (A-E), region (us-east/us-west/eu-west), status (active 70%/inactive 20%/pending 10%)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [ ]* 5.2 Write property test for ETL data generation schema and distribution
    - **Property 4: ETL Data Generation Schema and Distribution**
    - Verify generated CSV has correct columns, valid category/region values, and status distribution approximates 70/20/10 within statistical tolerance
    - **Validates: Requirements 8.2, 8.3**

- [x] 6. Checkpoint — Verify handlers, images, and payloads
  - Ensure all 4 handler.py files follow the dual-entrypoint pattern, Dockerfiles build successfully, and payload generation script produces correct S3 structure. Ask the user if questions arise.

- [x] 7. Implement deployment scripts
  - [x] 7.1 Create `scripts/deploy_lambda.sh` — deploy 16 Lambda functions with API Gateway HTTP APIs: iterate all 32 deployment matrix entries filtered to serverless, create function with correct memory (512/2048), 300s timeout, VPC attachment, env vars (DATA_BUCKET, PLATFORM=lambda, ARCHETYPE, DOCDB_ENDPOINT for enterprise-microservice), create HTTP API with POST /invoke and GET /health routes, prod stage with auto-deploy, record endpoints to `endpoints.txt`
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_
  - [x] 7.2 Create `scripts/deploy_fargate.sh` — deploy 16 Fargate services: iterate all container entries, create CloudWatch log group, register task definition with correct CPU/memory mapping (512MB→512 CPU/1024 MEM, 2048MB→2048 CPU/4096 MEM), create ALB target group with /health check, add path-based listener rule, create ECS service with desired count 1, record endpoints to `endpoints.txt`
    - Include DOCDB_ENDPOINT env var for enterprise-microservice task definitions
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_
  - [ ]* 7.3 Write property test for deployment naming convention round-trip
    - **Property 2: Deployment Naming Convention Round-Trip**
    - For any valid (archetype, memory, image_size, platform_suffix), generating and parsing the name recovers original values; `serverless`→`lambda`, `container`→`fargate`
    - **Validates: Requirements 9.1, 10.1, 12.1, 12.3**
  - [ ]* 7.4 Write property test for Fargate memory-to-CPU mapping
    - **Property 10: Fargate Memory-to-CPU Mapping**
    - 512MB→(512 CPU, 1024 MEM), 2048MB→(2048 CPU, 4096 MEM)
    - **Validates: Requirements 10.2, 10.3**
  - [x] 7.5 Create `scripts/generate_deployments_json.py` — parse `endpoints.txt` (format `{name}={url}`), extract archetype/platform/memory_mb/image_size from name, map `serverless`→`lambda` and `container`→`fargate`, output `deployments.json` with 32 entries
    - _Requirements: 12.1, 12.2, 12.3_
  - [x] 7.6 Create `scripts/validate_endpoints.py` — send test POST with small tier payload to each of 32 endpoints in `endpoints.txt`, mark PASS on HTTP 200, mark FAIL with error details on non-200 or connection error, print summary (passed/32 + failed list)
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

- [x] 8. Checkpoint — Verify deployment scripts
  - Ensure deployment scripts use correct naming convention, memory/CPU mappings, and all 32 entries are covered. Ask the user if questions arise.

- [x] 9. Implement load generator
  - [x] 9.1 Create `scripts/load_generator.py` — main orchestrator: accept `--config` and `--output` CLI args, load `deployments.json`, launch 32 threads (one per deployment) with 0.5s stagger, each thread runs 36 sequential blocks (4 freq × 3 CV × 3 duration tier), write per-deployment CSV with one row per block, log to `load_generator.log`
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5_
  - [x] 9.2 Implement Gamma distribution inter-arrival time generation — `shape = 1/CV²`, `scale = mean_interval/shape`, mean_interval = `1/rate_per_second`, rate derived from frequency map (1k=1000/86400, 10k=10000/86400, 50k=50000/86400, 100k=100000/86400)
    - _Requirements: 13.1, 13.2, 13.3, 13.4_
  - [x] 9.3 Implement block execution — 90-minute active load with Gamma-distributed inter-arrivals, 15-minute idle between blocks, 30s request timeout, increment error counter on non-200/timeout, write block summary row to CSV with all 18 columns
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6_
  - [x] 9.4 Implement wrk2 integration for enterprise-microservice — detect archetype, invoke wrk2 as subprocess with 4 threads, 50 connections, block duration, target rate; use Lua scripts (`lua/hotel_search.lua`, `lua/hotel_recommendation.lua`, `lua/hotel_booking.lua`); parse wrk2 stdout for p50/p95/p99 latency with unit conversion (us/ms/s → ms); write to same CSV schema
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5_
  - [x] 9.5 Create `lua/hotel_search.lua`, `lua/hotel_recommendation.lua`, `lua/hotel_booking.lua` — wrk2 Lua scripts for enterprise-microservice duration tiers (search-only, search+recommendation, full-booking)
    - _Requirements: 16.2_
  - [ ]* 9.6 Write property test for Gamma distribution inter-arrival times
    - **Property 5: Gamma Distribution Inter-Arrival Times**
    - For any valid (rate, cv), empirical mean ≈ 1/rate and empirical CV ≈ target cv within statistical tolerance
    - **Validates: Requirements 13.1, 13.2, 13.3, 13.4**
  - [ ]* 9.7 Write property test for block parameter completeness
    - **Property 6: Block Parameter Completeness**
    - 4 frequencies × 3 CVs × 3 tiers = exactly 36 unique tuples, no duplicates, no missing
    - **Validates: Requirements 14.1**
  - [ ]* 9.8 Write property test for payload tier mapping completeness
    - **Property 7: Payload Tier Mapping Completeness**
    - For any valid (archetype, tier), lookup returns non-empty dict with `payload_tier` key; enterprise-microservice also maps to valid Lua script path
    - **Validates: Requirements 14.4, 16.2**
  - [ ]* 9.9 Write property test for CSV block row round-trip
    - **Property 8: CSV Block Row Round-Trip**
    - Writing and reading back a block summary dict preserves all 18 field names and values
    - **Validates: Requirements 15.2, 15.3, 16.5**
  - [ ]* 9.10 Write property test for wrk2 latency parsing
    - **Property 9: wrk2 Latency Parsing**
    - Values ending in "us" → /1000, "ms" → unchanged, "s" → ×1000
    - **Validates: Requirements 16.4**

- [x] 10. Implement EC2 load generator instance setup
  - [x] 10.1 Create `scripts/06_setup_ec2.sh` — provision c5.2xlarge in private subnet with IAM instance profile (SSM, CloudWatch read, Cost Explorer read), install Python 3.11, numpy, requests, git, gcc, make, openssl-devel, build wrk2 from source at `/usr/local/bin/wrk2`, copy load generator files to instance
    - Access via SSM Session Manager only (no SSH key, no public IP)
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_

- [x] 11. Checkpoint — Verify load generator and EC2 setup
  - Ensure load generator handles all 4 archetypes correctly, wrk2 integration dispatches for enterprise-microservice only, and EC2 setup script installs all dependencies. Ask the user if questions arise.

- [x] 12. Implement data collection scripts
  - [x] 12.1 Create `scripts/query_cold_starts.py` — query CloudWatch Logs Insights for 16 Lambda log groups, filter REPORT lines with `@initDuration`, aggregate in 90-min bins, accept `--start-time`/`--end-time`/`--output` args, output CSV with columns: function_name, total_invocations, cold_start_count, avg_cold_start_ms, p95_cold_start_ms, avg_duration_ms, p95_duration_ms
    - Use `svc-` prefix function names with `-serverless` suffix, region us-east-2
    - _Requirements: 18.1, 18.2, 18.3, 18.4_
  - [x] 12.2 Create `scripts/query_latency.py` — query CloudWatch Metrics: Lambda `AWS/Lambda` Duration with ExtendedStatistics p50/p95/p99, Fargate `AWS/ApplicationELB` TargetResponseTime (convert s→ms), accept `--start-time`/`--end-time`/`--alb-arn-suffix`/`--output` args, output CSV with columns: function, platform, p50_ms, p95_ms, p99_ms
    - _Requirements: 19.1, 19.2, 19.3, 19.4_
  - [x] 12.3 Create `scripts/query_costs.py` — query Cost Explorer for daily costs grouped by service and usage type, filter to Lambda/ECS/API Gateway/ELB/CloudWatch, exclude S3 and data transfer, accept `--start-date`/`--end-date`/`--output` args, output CSV with columns: date, service, usage_type, cost_usd, unit
    - _Requirements: 20.1, 20.2, 20.3, 20.4_
  - [x] 12.4 Create `scripts/aggregate_results.py` — merge load generator CSVs + cold starts + latency + costs into single `results.csv` with 1,152 rows; set Fargate cold_start_rate/duration to 0.0; compute Lambda cold_start_rate_pct; add image_size_mb (slim=50, standard=250) and state_management (enterprise-microservice=1, others=0); output columns in specified order; warn if row count ≠ 1,152
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7_
  - [ ]* 12.5 Write property test for results aggregation invariants
    - **Property 11: Results Aggregation Invariants**
    - Verify: 1,152 rows from 32×36; Fargate cold starts = 0; Lambda cold_start_rate_pct formula; image_size mapping; state_management mapping
    - **Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5**

- [x] 13. Implement training data preparation
  - [x] 13.1 Create `scripts/prepare_training_data.py` — load `results.csv`, add derived features (invocation_frequency_numeric, duration_tier_numeric, log_invocations, sustained_load), stratified 80/20 split by archetype with random_state=42, write `train.csv` and `test.csv`, print archetype distribution for both sets
    - _Requirements: 22.1, 22.2, 22.3, 22.4_
  - [ ]* 13.2 Write property test for feature engineering correctness
    - **Property 12: Feature Engineering Correctness**
    - 1k→1000, 10k→10000, 50k→50000, 100k→100000; small→500, medium→2000, large→5000; log_invocations = ln(freq_numeric); sustained_load = freq_numeric × dur_numeric / 1e6
    - **Validates: Requirements 22.3**

- [x] 14. Implement cleanup script
  - [x] 14.1 Create `scripts/cleanup.sh` — delete all resources in reverse dependency order: Lambda functions (svc- prefix), API Gateways (svc- prefix), ECS services + task definitions, ALB target groups + ALB, DocumentDB cluster + subnet group, ECR repos (svc-experiment/ with --force), S3 bucket (empty + delete), NAT GW + EIP + IGW, subnets + route tables + SG + VPC, IAM roles (detach policies + delete), EC2 instance + instance profile; warn if results.csv not backed up
    - Use `|| true` on delete commands, wait for NAT GW deletion before releasing EIP
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8, 23.9, 23.10_

- [x] 15. Implement experiment monitoring commands
  - [x] 15.1 Create `scripts/monitor_experiment.sh` — provide commands to check load generator process status, tail log file, count completed blocks (expected 1,152), CloudWatch CLI commands for Lambda invocation counts and error rates, ECS CLI commands for Fargate running task counts, force new deployment command for stopped services
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5_

- [x] 16. Final checkpoint — Ensure all tests pass
  - Run all property-based tests and unit tests. Verify all scripts are syntactically correct and reference consistent naming conventions. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation between major phases
- Property tests validate the 12 universal correctness properties from the design document using `hypothesis`
- All bash scripts use `set -euo pipefail` and source `experiment-env.sh`
- All Python scripts target Python 3.11
- Handler code preserves original benchmark logic from SeBS and DeathStarBench references
