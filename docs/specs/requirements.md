# Requirements Document

## Introduction

This document specifies the requirements for a serverless vs. container benchmark experiment platform designed to generate empirical training data. The platform deploys 32 parallel endpoints (16 AWS Lambda functions behind API Gateway HTTP APIs + 16 AWS ECS Fargate services behind an internal ALB), runs a Python load generator on EC2 (accessed via SSM) that orchestrates 36 sequential load blocks per deployment, and collects performance/cost metrics into a 1,152-record dataset for training four regression models (Lambda cost, Fargate cost, cold start rate, p95 latency).

## Glossary

- **Platform**: The experiment system as a whole, encompassing all scripts, infrastructure, and tooling
- **Load_Generator**: The Python script (`load_generator.py`) running on an EC2 c5.2xlarge instance that sends HTTP requests to all 32 endpoints in parallel using Python threading
- **Deployment**: A single endpoint configuration consisting of one archetype, one memory level, one image size, and one platform (Lambda or Fargate) — 32 total
- **Block**: One 90-minute active load period followed by a 15-minute idle period, representing one combination of invocation frequency, traffic CV, and duration tier — 36 per deployment
- **Archetype**: One of four workload types: Event-Driven API, Batch Transform, ML Inference, or Enterprise Microservice
- **Serverless_Endpoint**: An AWS Lambda function fronted by an API Gateway HTTP API
- **Container_Endpoint**: An AWS ECS Fargate service fronted by an ALB path-based routing rule
- **Aggregator**: The Python script (`aggregate_results.py`) that merges load generator CSVs, cold start data, latency metrics, and cost data into a single `results.csv`
- **Cold_Start_Extractor**: The Python script (`query_cold_starts.py`) that queries CloudWatch Logs Insights for Lambda Init Duration metrics
- **Latency_Collector**: The Python script (`query_latency.py`) that pulls p50/p95/p99 latency from CloudWatch Metrics
- **Cost_Collector**: The Python script (`query_costs.py`) that retrieves per-service cost data from AWS Cost Explorer
- **Training_Data_Preparer**: The Python script (`prepare_training_data.py`) that performs train/test split and feature engineering
- **Naming_Convention**: The resource naming pattern `svc-{archetype}-{memory}-{imagesize}-{platform}` using `svc` prefix, `serverless` for Lambda, and `container` for Fargate
- **CV**: Coefficient of Variation (StdDev / Mean) of request inter-arrival times controlling traffic burstiness
- **Duration_Tier**: One of three payload complexity levels (`small`, `medium`, `large`) controlling execution duration (~500ms, ~2s, ~5s)
- **DocumentDB**: Amazon DocumentDB (MongoDB-compatible) used as the backing store for the Enterprise Microservice archetype
- **Payload_Generator**: The Python script (`generate_payloads.py`) that creates synthetic test data and uploads it to S3
- **Endpoint_Validator**: The Python script (`validate_endpoints.py`) that verifies all 32 endpoints return HTTP 200
## Requirements

### Requirement 1: VPC and Network Infrastructure

**User Story:** As a researcher, I want a properly configured VPC with private subnets and a NAT Gateway in us-east-2, so that Lambda functions, Fargate services, and the EC2 load generator operate in a fully private network with outbound-only internet access for AWS service calls.

#### Acceptance Criteria

1. THE Platform SHALL create a VPC with CIDR block 10.0.0.0/16 in us-east-2
2. THE Platform SHALL create one public subnet (for the NAT Gateway only — no other resources) and two private subnets (in two availability zones) for the internal ALB, Lambda functions, Fargate tasks, DocumentDB, and the EC2 load generator
3. THE Platform SHALL create an Internet Gateway attached to the VPC and a NAT Gateway in the public subnet with an Elastic IP, providing outbound-only internet access from the private subnets
4. THE Platform SHALL create a security group named `svc-experiment-sg` allowing HTTP inbound on ports 80 and 8080, and all internal traffic within the security group — no resources shall have public IP addresses or be directly accessible from the internet
5. THE Platform SHALL persist all resource IDs (VPC, subnets, security group, IGW, NAT Gateway, EIP, route tables) to an `experiment-env.sh` file for use by subsequent scripts

### Requirement 2: IAM Roles

**User Story:** As a researcher, I want IAM roles with appropriate permissions for Lambda and Fargate, so that all 32 endpoints can access S3 payloads and write to CloudWatch.

#### Acceptance Criteria

1. THE Platform SHALL create a Lambda execution role named `svc-lambda-execution-role` with trust policy for `lambda.amazonaws.com`
2. THE Platform SHALL attach AWSLambdaBasicExecutionRole, AWSLambdaVPCAccessExecutionRole, and AmazonS3FullAccess policies to the Lambda execution role
3. THE Platform SHALL create a Fargate task execution role named `svc-fargate-execution-role` with trust policy for `ecs-tasks.amazonaws.com`
4. THE Platform SHALL attach AmazonECSTaskExecutionRolePolicy and AmazonS3FullAccess policies to the Fargate execution role
5. THE Platform SHALL persist both role ARNs to `experiment-env.sh`

### Requirement 3: S3 Bucket and ECR Repositories

**User Story:** As a researcher, I want an S3 bucket for payload data and ECR repositories for container images, so that all archetypes have accessible storage for their test data and deployable images.

#### Acceptance Criteria

1. THE Platform SHALL create an S3 bucket named `svc-experiment-data-{ACCOUNT_ID}` in us-east-2
2. THE Platform SHALL create four ECR repositories, one per archetype: `svc-experiment/event-driven-api`, `svc-experiment/batch-transform`, `svc-experiment/ml-inference`, `svc-experiment/enterprise-microservice`
3. THE Platform SHALL persist the bucket name, account ID, and ECR registry URL to `experiment-env.sh`

### Requirement 4: ECS Cluster and ALB

**User Story:** As a researcher, I want an ECS cluster and internal Application Load Balancer, so that all 16 Fargate services can be deployed and routed to via path-based rules within the private VPC.

#### Acceptance Criteria

1. THE Platform SHALL create an ECS cluster named `svc-experiment-cluster` with FARGATE capacity provider
2. THE Platform SHALL create an internal ALB named `svc-experiment-alb` in the private subnets
3. THE Platform SHALL create a default HTTP listener on port 80 returning a 404 fixed response
4. THE Platform SHALL persist the ALB ARN, ALB DNS name, and listener ARN to `experiment-env.sh`

### Requirement 5: DocumentDB for Enterprise Microservice Archetype

**User Story:** As a researcher, I want an Amazon DocumentDB cluster provisioned in the private subnets, so that the Enterprise Microservice archetype has a MongoDB-compatible backing store for stateful operations.

#### Acceptance Criteria

1. THE Platform SHALL create a DocumentDB subnet group using the two private subnets
2. THE Platform SHALL create a DocumentDB cluster in us-east-2 within the private subnets, accessible from the experiment security group
3. THE Platform SHALL persist the DocumentDB cluster endpoint to `experiment-env.sh`
4. WHEN the Enterprise Microservice archetype handler connects to DocumentDB, THE handler SHALL use the cluster endpoint from the `DOCDB_ENDPOINT` environment variable

### Requirement 6: Workload Handler Implementations

**User Story:** As a researcher, I want handler implementations for all four archetypes that share identical application logic across Lambda and Fargate, so that the only variable between platforms is the compute paradigm.

#### Acceptance Criteria

1. THE Event-Driven API handler SHALL accept a JSON payload with `payload_tier` and `s3_key` fields, download the image from S3, and perform tier-appropriate processing (small: resize only; medium: resize + format conversion + EXIF extraction; large: resize + multi-format compression with 9 variants + watermarking)
2. THE Batch Transform handler SHALL accept a JSON payload with `payload_tier` and `s3_key` fields, download CSV data from S3, and perform tier-appropriate processing (small: 10K rows single-column aggregation; medium: 100K rows multi-column aggregation + type casting + null handling; large: 1M rows full pipeline with Parquet export)
3. THE ML Inference handler SHALL accept a JSON payload with `payload_tier` and `batch_size` fields, load a pre-trained model from the container image, and perform image classification (small: batch=1 MobileNetV2; medium: batch=4 ResNet-50; large: batch=8 ResNet-50)
4. THE Enterprise Microservice handler SHALL expose three endpoints (`GET /hotels`, `GET /recommendations`, `POST /reservation`) backed by DocumentDB, implementing search, recommendation, and full booking operations
5. WHILE running on Fargate, each handler SHALL start a Flask HTTP server on port 8080 with a `POST /invoke` endpoint and a `GET /health` endpoint
6. WHILE running on Lambda, each handler SHALL export a `handler(event, context)` function compatible with the Lambda Python 3.11 runtime
7. THE handler application logic in `handler.py` SHALL be identical for both Lambda and Fargate deployments of the same archetype, differing only in the entrypoint mechanism

### Requirement 7: Container Image Builds

**User Story:** As a researcher, I want slim (~50MB) and standard (~250MB) container image variants for each archetype, so that the experiment can measure the cold start impact of image size.

#### Acceptance Criteria

1. THE Platform SHALL build two Dockerfile variants per archetype: `Dockerfile.slim` (minimal dependencies, ~50MB target) and `Dockerfile.standard` (additional enterprise libraries, ~250MB target)
2. THE Platform SHALL use `public.ecr.aws/lambda/python:3.11` as the base image for Lambda targets and `python:3.11-slim` (slim) or `python:3.11` (standard) for Fargate targets
3. THE Platform SHALL build and push 16 container images total (4 archetypes × 2 size variants × 2 platform targets) to the corresponding ECR repositories
4. THE Platform SHALL tag images using the pattern `{platform}-{size}` (e.g., `serverless-slim`, `serverless-standard`, `container-slim`, `container-standard`)
5. THE ML Inference standard image SHALL include the pre-trained ResNet-50 model weights (~98MB) baked into the container image

### Requirement 8: Payload Generation and Upload

**User Story:** As a researcher, I want synthetic test payloads uploaded to S3 for each archetype and duration tier, so that the load generator can reference them during experiments.

#### Acceptance Criteria

1. THE Payload_Generator SHALL create and upload Event-Driven API payloads to S3: a ~50KB JPEG at `payloads/event-driven-api/small/sample.jpg`, a ~500KB PNG at `payloads/event-driven-api/medium/sample.png`, and a ~2MB TIFF at `payloads/event-driven-api/large/sample.tiff`
2. THE Payload_Generator SHALL generate and upload Batch Transform CSV datasets to S3: 10K rows at `payloads/batch-transform/small/data.csv`, 100K rows at `payloads/batch-transform/medium/data.csv`, and 1M rows at `payloads/batch-transform/large/data.csv`
3. THE Payload_Generator SHALL generate ETL CSV data with columns: `id`, `value_a` (normal distribution, mean=100, std=25), `value_b` (normal distribution, mean=50, std=10), `category` (A/B/C/D/E), `region` (us-east/us-west/eu-west), `status` (active 70%/inactive 20%/pending 10%)
4. THE Payload_Generator SHALL upload ML Inference test images to S3 for batch processing at each tier
5. THE Payload_Generator SHALL seed the DocumentDB database with hotel, user, and reservation data for the Enterprise Microservice archetype


### Requirement 9: Lambda Deployments (16 Serverless Endpoints)

**User Story:** As a researcher, I want 16 Lambda functions deployed behind API Gateway HTTP APIs, so that the serverless platform is represented across all archetype/memory/image-size combinations.

#### Acceptance Criteria

1. THE Platform SHALL create 16 Lambda functions following the Naming_Convention: `svc-{archetype}-{memory}-{imagesize}-serverless` (e.g., `svc-event-driven-api-512mb-slim-serverless`)
2. WHEN deploying a Lambda function, THE Platform SHALL configure it with the correct memory allocation (512 or 2048 MB), a 300-second timeout, VPC attachment to the private subnets, and environment variables (`DATA_BUCKET`, `PLATFORM=lambda`, `ARCHETYPE`)
3. WHEN deploying a Lambda function, THE Platform SHALL create an API Gateway HTTP API with a `POST /invoke` route and a `GET /health` route, both integrated with the Lambda function using AWS_PROXY integration and payload format version 2.0
4. WHEN deploying a Lambda function, THE Platform SHALL create a `prod` stage with auto-deploy enabled and grant API Gateway permission to invoke the function
5. THE Platform SHALL record each Lambda endpoint URL in the format `https://{API_ID}.execute-api.us-east-2.amazonaws.com/prod/invoke` to an `endpoints.txt` file
6. WHEN deploying Enterprise Microservice Lambda functions, THE Platform SHALL include the `DOCDB_ENDPOINT` environment variable pointing to the DocumentDB cluster endpoint

### Requirement 10: Fargate Deployments (16 Container Endpoints)

**User Story:** As a researcher, I want 16 Fargate services deployed behind an ALB with path-based routing, so that the container platform is represented across all archetype/memory/image-size combinations.

#### Acceptance Criteria

1. THE Platform SHALL create 16 ECS Fargate services following the Naming_Convention: `svc-{archetype}-{memory}-{imagesize}-container` (e.g., `svc-event-driven-api-512mb-slim-container`)
2. WHEN deploying a Fargate service with 512MB memory, THE Platform SHALL configure the task definition with 512 CPU units (0.5 vCPU) and 1024 MB memory
3. WHEN deploying a Fargate service with 2GB memory, THE Platform SHALL configure the task definition with 2048 CPU units (2 vCPU) and 4096 MB memory
4. WHEN deploying a Fargate service, THE Platform SHALL create a CloudWatch log group at `/ecs/{service-name}`, register a task definition with awsvpc networking, and create the service with desired count of 1
5. WHEN deploying a Fargate service, THE Platform SHALL create an ALB target group with health check path `/health`, and add a path-based listener rule routing `/{service-name}/*` to the target group
6. THE Platform SHALL record each Fargate endpoint URL in the format `http://{ALB_DNS}/{service-name}/invoke` to the `endpoints.txt` file
7. WHEN deploying Enterprise Microservice Fargate services, THE Platform SHALL include the `DOCDB_ENDPOINT` environment variable in the task definition

### Requirement 11: Endpoint Validation

**User Story:** As a researcher, I want to validate all 32 endpoints are healthy before starting experiments, so that no data is lost due to misconfigured deployments.

#### Acceptance Criteria

1. WHEN the Endpoint_Validator runs, THE Endpoint_Validator SHALL send a test POST request with a `small` tier payload to each of the 32 endpoints listed in `endpoints.txt`
2. WHEN an endpoint returns HTTP 200, THE Endpoint_Validator SHALL mark the endpoint as PASS
3. IF an endpoint returns a non-200 status code or a connection error, THEN THE Endpoint_Validator SHALL mark the endpoint as FAIL and report the error details
4. WHEN validation completes, THE Endpoint_Validator SHALL print a summary showing the count of passed endpoints out of 32 and list all failed endpoints

### Requirement 12: Deployments JSON Generation

**User Story:** As a researcher, I want a `deployments.json` file generated from `endpoints.txt`, so that the load generator has a structured configuration mapping each deployment to its URL and metadata.

#### Acceptance Criteria

1. WHEN the deployments JSON generator processes `endpoints.txt`, THE generator SHALL parse each line in the format `{name}={url}` and extract the archetype, platform, memory_mb, and image_size from the deployment name
2. THE generator SHALL produce a `deployments.json` file containing an array of 32 objects, each with fields: `name`, `url`, `archetype`, `platform`, `memory_mb`, and `image_size`
3. THE generator SHALL map platform suffixes correctly: `serverless` in the name maps to `"platform": "lambda"` and `container` maps to `"platform": "fargate"` in the JSON output

### Requirement 13: Load Generator — Inter-Arrival Time Distribution

**User Story:** As a researcher, I want the load generator to use Gamma-distributed inter-arrival times, so that traffic burstiness (CV) is precisely controlled as an experimental variable.

#### Acceptance Criteria

1. THE Load_Generator SHALL generate inter-arrival times using a Gamma distribution with shape parameter `1/CV²` and scale parameter `mean_interval/shape`
2. WHEN CV equals 0.5, THE Load_Generator SHALL produce near-steady traffic with low variance
3. WHEN CV equals 4.0, THE Load_Generator SHALL produce highly bursty traffic with high variance
4. THE Load_Generator SHALL compute the mean inter-arrival interval as `1 / rate_per_second` where rate_per_second is derived from the invocation frequency (1K=1000/86400, 10K=10000/86400, 50K=50000/86400, 100K=100000/86400 requests per second)

### Requirement 14: Load Generator — Block Execution

**User Story:** As a researcher, I want the load generator to execute 36 sequential blocks per deployment with controlled parameters, so that every combination of frequency, CV, and duration tier is measured.

#### Acceptance Criteria

1. THE Load_Generator SHALL execute 36 blocks per deployment: 4 invocation frequencies × 3 CV levels × 3 duration tiers, in nested iteration order (frequency → CV → duration tier)
2. WHEN executing a block, THE Load_Generator SHALL send HTTP POST requests to the deployment endpoint for 90 minutes (5,400 seconds) of active load
3. WHEN a block completes, THE Load_Generator SHALL idle for 15 minutes (900 seconds) before starting the next block, to allow Lambda execution environments to recycle
4. THE Load_Generator SHALL send the archetype-appropriate payload body for each duration tier as defined in the PAYLOAD_TIERS configuration
5. THE Load_Generator SHALL set a 30-second timeout on each individual HTTP request
6. WHEN a request returns a non-200 status code or times out, THE Load_Generator SHALL increment the error counter for the block and continue sending subsequent requests

### Requirement 15: Load Generator — Parallel Execution and Logging

**User Story:** As a researcher, I want all 32 deployments to run their blocks simultaneously, so that the full experiment completes within the 7-day window.

#### Acceptance Criteria

1. THE Load_Generator SHALL launch one Python thread per deployment (32 threads total), with a 0.5-second stagger between thread starts
2. THE Load_Generator SHALL write per-deployment CSV files to the output directory, with one row per completed block
3. WHEN writing a block row, THE Load_Generator SHALL include columns: `deployment_name`, `archetype`, `platform`, `memory_mb`, `image_size`, `block_index`, `invocation_frequency`, `traffic_cv`, `duration_tier`, `block_start_utc`, `total_requests`, `error_count`, `error_rate_pct`, `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`, `mean_latency_ms`, `throughput_rps`
4. THE Load_Generator SHALL write timestamped log entries to `load_generator.log` including thread name, block start/end markers, and error details
5. THE Load_Generator SHALL accept `--config` (path to deployments.json) and `--output` (output directory) command-line arguments

### Requirement 16: wrk2 Integration for Enterprise Microservice

**User Story:** As a researcher, I want the load generator to use wrk2 with Lua scripts for Enterprise Microservice deployments, so that this stateful archetype is tested with an appropriate HTTP benchmarking tool.

#### Acceptance Criteria

1. WHEN the Load_Generator encounters a deployment with archetype `enterprise-microservice`, THE Load_Generator SHALL invoke wrk2 as a subprocess instead of using the Python HTTP client
2. THE Load_Generator SHALL use three Lua scripts mapping to duration tiers: `lua/hotel_search.lua` (small/search-only), `lua/hotel_recommendation.lua` (medium/search+recommendation), `lua/hotel_booking.lua` (large/full-booking)
3. THE Load_Generator SHALL invoke wrk2 with 4 threads, 50 connections, the block duration (5,400 seconds), and the target request rate derived from the invocation frequency
4. WHEN wrk2 completes, THE Load_Generator SHALL parse the wrk2 stdout output to extract p50, p95, and p99 latency values, converting units (us/ms/s) to milliseconds
5. THE Load_Generator SHALL write the wrk2 block results to the same per-deployment CSV format as Python HTTP blocks, ensuring consistent schema across all archetypes

### Requirement 17: EC2 Load Generator Instance Setup

**User Story:** As a researcher, I want the EC2 load generator instance provisioned in a private subnet and accessible via SSM Session Manager, so that the experiment can run unattended for 7 days without requiring internet-facing infrastructure.

#### Acceptance Criteria

1. THE Platform SHALL provision a c5.2xlarge EC2 instance in a private subnet of the experiment VPC in us-east-2, with an IAM instance profile granting SSM Session Manager access, CloudWatch read access, and Cost Explorer read access
2. THE Platform SHALL install Python 3.11, numpy, requests, git, gcc, make, and openssl-devel on the EC2 instance
3. THE Platform SHALL build and install wrk2 from source on the EC2 instance at `/usr/local/bin/wrk2`
4. THE Platform SHALL copy `load_generator.py`, `deployments.json`, and the `lua/` directory to the EC2 instance
5. THE Platform SHALL access the EC2 instance via SSM Session Manager (no SSH key pair or public IP required)

### Requirement 18: Cold Start Data Extraction

**User Story:** As a researcher, I want cold start metrics extracted from CloudWatch Logs Insights for all 16 Lambda functions, so that cold start frequency and duration are captured per block.

#### Acceptance Criteria

1. WHEN the Cold_Start_Extractor runs, THE Cold_Start_Extractor SHALL query CloudWatch Logs Insights for each of the 16 Lambda function log groups using the REPORT line filter with `@initDuration` field
2. THE Cold_Start_Extractor SHALL aggregate results in 90-minute bins matching the block duration, computing: total invocations, cold start count, average cold start duration (ms), and p95 cold start duration (ms)
3. THE Cold_Start_Extractor SHALL accept `--start-time`, `--end-time` (ISO format UTC), and `--output` command-line arguments
4. THE Cold_Start_Extractor SHALL write results to a CSV file with columns: `function_name`, `total_invocations`, `cold_start_count`, `avg_cold_start_ms`, `p95_cold_start_ms`, `avg_duration_ms`, `p95_duration_ms`

### Requirement 19: Latency Metrics Collection

**User Story:** As a researcher, I want p50/p95/p99 latency percentiles collected from CloudWatch Metrics for all 32 deployments, so that platform-level latency comparison is available.

#### Acceptance Criteria

1. WHEN collecting Lambda latency, THE Latency_Collector SHALL query the `AWS/Lambda` namespace `Duration` metric with ExtendedStatistics for p50, p95, and p99 per function
2. WHEN collecting Fargate latency, THE Latency_Collector SHALL query the `AWS/ApplicationELB` namespace `TargetResponseTime` metric per target group, converting seconds to milliseconds
3. THE Latency_Collector SHALL accept `--start-time`, `--end-time`, `--alb-arn-suffix`, and `--output` command-line arguments
4. THE Latency_Collector SHALL write results to a CSV file with columns: `function`, `platform`, `p50_ms`, `p95_ms`, `p99_ms`

### Requirement 20: Cost Data Collection

**User Story:** As a researcher, I want per-service cost data from AWS Cost Explorer, so that total cost of ownership (TCO) can be computed per deployment configuration.

#### Acceptance Criteria

1. THE Cost_Collector SHALL query AWS Cost Explorer for daily costs grouped by service and usage type, filtered to: AWS Lambda, Amazon Elastic Container Service, Amazon API Gateway, AWS Elastic Load Balancing, and AmazonCloudWatch
2. THE Cost_Collector SHALL accept `--start-date`, `--end-date` (YYYY-MM-DD format), and `--output` command-line arguments
3. THE Cost_Collector SHALL write results to a CSV file with columns: `date`, `service`, `usage_type`, `cost_usd`, `unit`
4. THE Cost_Collector SHALL exclude S3 storage and data transfer costs from the output, as these are identical across platforms

### Requirement 21: Results Aggregation

**User Story:** As a researcher, I want all data sources merged into a single results.csv with 1,152 block-level records, so that the dataset is ready for regression model training.

#### Acceptance Criteria

1. THE Aggregator SHALL merge load generator per-deployment CSVs, cold start data, latency metrics, and cost data into a single `results.csv`
2. THE Aggregator SHALL produce 1,152 rows (32 deployments × 36 blocks) in the output
3. WHEN merging cold start data for Fargate deployments, THE Aggregator SHALL set `cold_start_rate_pct` and `cold_start_duration_ms` to 0.0
4. THE Aggregator SHALL compute `cold_start_rate_pct` as `(cold_start_count / total_requests) × 100` for Lambda deployments
5. THE Aggregator SHALL add derived columns: `image_size_mb` (slim=50, standard=250) and `state_management` (enterprise-microservice=1, others=0)
6. THE Aggregator SHALL output columns in order: `deployment_name`, `archetype`, `platform`, `memory_mb`, `image_size_mb`, `state_management`, `invocation_frequency`, `traffic_cv`, `duration_tier`, `block_start_utc`, `total_requests`, `error_rate_pct`, `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`, `mean_latency_ms`, `throughput_rps`, `cold_start_rate_pct`, `cold_start_duration_ms`
7. IF the final record count does not equal 1,152, THEN THE Aggregator SHALL print a warning indicating the mismatch

### Requirement 22: Training Data Preparation

**User Story:** As a researcher, I want the results split into train/test sets with derived features, so that the four regression models can be trained and evaluated.

#### Acceptance Criteria

1. THE Training_Data_Preparer SHALL split `results.csv` into 80% training and 20% test sets using stratified sampling by archetype with random_state=42
2. THE Training_Data_Preparer SHALL write `train.csv` and `test.csv` to the specified output directory
3. THE Training_Data_Preparer SHALL add derived feature columns: `invocation_frequency_numeric` (1k→1000, 10k→10000, 50k→50000, 100k→100000), `duration_tier_numeric` (small→500, medium→2000, large→5000), `log_invocations` (natural log of invocation_frequency_numeric), and `sustained_load` (invocation_frequency_numeric × duration_tier_numeric / 1e6)
4. THE Training_Data_Preparer SHALL print the archetype distribution for both training and test sets to confirm proportional representation

### Requirement 23: Resource Cleanup

**User Story:** As a researcher, I want a cleanup script that deletes all experiment resources, so that AWS charges stop after data collection is complete.

#### Acceptance Criteria

1. WHEN the cleanup script runs, THE Platform SHALL delete all 16 Lambda functions matching the `svc-` prefix
2. WHEN the cleanup script runs, THE Platform SHALL delete all API Gateway HTTP APIs matching the `svc-` prefix
3. WHEN the cleanup script runs, THE Platform SHALL scale down and delete all 16 ECS services in the `svc-experiment-cluster`, then deregister their task definitions
4. WHEN the cleanup script runs, THE Platform SHALL delete all ALB target groups matching the `svc-` prefix, then delete the ALB
5. WHEN the cleanup script runs, THE Platform SHALL delete all ECR repositories under `svc-experiment/` with `--force` flag
6. WHEN the cleanup script runs, THE Platform SHALL empty and delete the S3 bucket
7. WHEN the cleanup script runs, THE Platform SHALL delete the NAT Gateway, release the Elastic IP, detach and delete the Internet Gateway, then delete subnets, route tables, security group, and VPC
8. WHEN the cleanup script runs, THE Platform SHALL detach all policies from and delete both IAM roles (`svc-lambda-execution-role` and `svc-fargate-execution-role`)
9. WHEN the cleanup script runs, THE Platform SHALL delete the DocumentDB cluster and subnet group
10. IF `results.csv` has not been confirmed as backed up, THEN THE cleanup script SHALL print a warning before proceeding

### Requirement 24: Experiment Monitoring

**User Story:** As a researcher, I want monitoring commands and guidance for the 7-day experiment run, so that I can detect and recover from failures without losing data.

#### Acceptance Criteria

1. THE Platform SHALL provide commands to check the load generator process status, tail the log file, and count completed blocks (expected: 1,152 at completion)
2. THE Platform SHALL provide CloudWatch CLI commands to check Lambda invocation counts and error rates per function
3. THE Platform SHALL provide ECS CLI commands to verify Fargate service running task counts
4. IF a Fargate service stops during the experiment, THEN THE Platform SHALL provide a command to force a new deployment of the service
5. IF a load generator thread dies, THEN THE Load_Generator SHALL continue running all remaining threads unaffected, and the missing blocks SHALL be noted as absent from results
