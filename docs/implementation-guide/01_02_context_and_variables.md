
# Experiment Implementation Guide — File 1 of 8
# Context, Background, and Experimental Variables

---

## Table of Contents (Full Guide)

- File 1: Context, Background, and Experimental Variables (Sections 1–2)
- File 2: The Four Workload Archetypes (Section 3)
- File 3: Infrastructure Setup and Container Images (Sections 4–5)
- File 4: Deploying the 32 Endpoints and Load Generator (Sections 6–7)
- File 5: Running Experiments, Data Collection, Training Data Prep, and Cleanup (Sections 8–11 + Appendix A)

---

## 1. Context and Background

### 1.1 What the Research Paper Is About

This research paper proposes a **predictive decision framework** to help cloud architects choose between **AWS Lambda (serverless / FaaS)** and **AWS ECS Fargate (containers / CaaS)** for compute workloads. The framework uses regression models trained on empirical benchmark data to predict cost, latency, and cold start behavior for a given workload, then recommends the better platform.

The paper targets the **IEEE Cloud Computing Conference**. The core novelty is the first empirically calibrated multi-dimensional predictive framework for FaaS vs. CaaS placement using identical containerized benchmark deployments across both paradigms.

### 1.2 Why These Experiments Are Needed

The regression models that power the decision framework must be trained on real empirical data. The experiments generate this training dataset. After the 7-day data collection window, the resulting **1,152 block-level records** will be used to:

1. Train four regression models: Lambda cost, Fargate cost, cold start rate, and p95 latency
2. Validate model prediction accuracy using an 80/20 train/test split
3. Generate multi-dimensional break-even surfaces showing when Lambda vs. Fargate is more cost-effective
4. Demonstrate that multi-dimensional models outperform single-variable heuristics (e.g., the "500K invocations/month" rule)

### 1.3 What You Will Build

1. **32 parallel deployments**: 16 Lambda functions (behind API Gateway HTTP APIs) + 16 Fargate services (behind an ALB)
2. A **Python load generator** running on EC2 (c5.2xlarge) that orchestrates all 36 sequential load blocks per deployment
3. **Payload files in S3** representing three duration tiers for each archetype
4. A **data collection script** that extracts and aggregates cost and performance metrics from CloudWatch, X-Ray, and Cost Explorer

### 1.4 High-Level Architecture

```
+----------------------------------------------------------------------+
|             LOAD GENERATOR  (EC2 c5.2xlarge, us-east-1)             |
|  Controls: invocation frequency, traffic burstiness (CV),           |
|            payload/operation complexity tier (execution duration)    |
|  Runs 36 sequential blocks per deployment (all 32 in parallel)      |
+------------------------+-----------------------------+---------------+
                         |                             |
                         v                             v
+------------------------+---+         +---------------+---------------+
|   AWS Lambda (16 fns)      |         |  ECS Fargate  (16 services)  |
|   API Gateway HTTP API     |         |  Application Load Balancer   |
|   4 archetypes             |         |  4 archetypes                |
|   x 2 memory levels        |         |  x 2 memory levels           |
|   x 2 image sizes          |         |  x 2 image sizes             |
+----------------------------+         +------------------------------+
                         |                             |
                         +-------------+---------------+
                                       v
+----------------------------------------------------------------------+
|                      METRICS COLLECTION                              |
|  CloudWatch Logs    CloudWatch Metrics    AWS X-Ray                  |
|  (Init Duration, billed duration, p50/p95/p99 latency)              |
|  AWS Cost Explorer (compute, API GW / ALB, CloudWatch Logs costs)   |
+----------------------------------------------------------------------+
```

### 1.5 Expected Outputs

After the experiment completes, you will produce:
- A CSV file with **1,152 block-level records** (32 deployments × 36 blocks), each containing measured cost, latency percentiles, cold start rate, and cold start duration
- Raw CloudWatch Logs exports for reproducibility verification
- A cost breakdown spreadsheet showing TCO per configuration

---

## 2. The Six Experimental Variables

These six dimensions are systematically varied across the experiment. Understanding each is critical before building anything.

- **Variables 1–3** are controlled at runtime by the load generator (no redeployment needed)
- **Variables 4–6** require separate deployments at infrastructure setup time

| # | Variable | What It Measures | How Controlled | Levels | Values |
|---|---|---|---|---|---|
| 1 | **Invocation Frequency** | Number of requests per day sent to the endpoint | Load generator adjusts request rate (req/sec) | 4 | 1K / 10K / 50K / 100K requests/day |
| 2 | **Traffic Burstiness (CV)** | Coefficient of Variation of inter-arrival times. CV=0.5 = steady; CV=2.0 = moderate; CV=4.0 = very bursty. CV = StdDev / Mean. | Load generator uses Gamma distribution for inter-arrival times | 3 | 0.5 / 2.0 / 4.0 |
| 3 | **Execution Duration** | How long each function invocation takes (measured at p95). Controlled via payload complexity or operation type per archetype | Load generator sends different payload sizes or operation types in the request body | 3 | ~500ms / ~2s / ~5s |
| 4 | **Memory Allocation** | RAM allocated to the Lambda function or Fargate task. Higher memory also increases vCPU allocation proportionally | Set at deployment time in Lambda function config or Fargate task definition | 2 | 512MB / 2GB |
| 5 | **Container Image Size** | Size of the Docker image. Larger images increase cold start duration (Lambda must pull the image). Primarily affects the cold start regression model | Built at Docker image creation time: slim Dockerfile (~50MB) vs. standard Dockerfile (~250MB) | 2 | 50MB (slim) / 250MB (standard) |
| 6 | **Platform** | The compute platform being tested. Same Docker image deployed to both platforms for a fair comparison | Separate deployments: Lambda function + API Gateway vs. Fargate service + ALB | 2 | AWS Lambda / ECS Fargate |

> **NOTE**: State management is a 7th characteristic captured implicitly by archetype selection: Archetypes 1–3 are stateless; Archetype 4 (Hotel Reservation) is stateful. No additional deployment is needed for this dimension.

### 2.1 Understanding CV (Coefficient of Variation)

CV = Standard Deviation / Mean of the request inter-arrival times.

- **CV = 0.5** → Traffic arrives fairly steadily and predictably (e.g., a scheduled batch job)
- **CV = 2.0** → Moderate variability (e.g., a typical web API with normal daily fluctuations)
- **CV = 4.0** → Highly bursty (e.g., a flash sale, viral event)

A Poisson arrival process has CV = 1.0 by definition. Values above 1.0 indicate burstier-than-Poisson traffic. We use a **Gamma distribution** to generate inter-arrival times because it allows independent control of the mean (request rate) and CV (burstiness).

### 2.2 Total Configuration Space

```
4 archetypes
× 2 memory levels (512MB, 2GB)
× 2 image sizes (slim, standard)
× 2 platforms (Lambda, Fargate)
= 32 parallel deployments

Each deployment runs:
4 frequencies × 3 CV levels × 3 duration tiers = 36 sequential blocks

Total records: 32 × 36 = 1,152 block-level records
```
