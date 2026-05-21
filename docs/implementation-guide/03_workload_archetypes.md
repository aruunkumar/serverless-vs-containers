
# Experiment Implementation Guide — File 2 of 8
# The Four Workload Archetypes

> **Cross-reference**: Read File 1 (context and variables) before this file.

---

## 3. The Four Workload Archetypes

Four representative workload archetypes were selected from two established open-source benchmark suites. Each archetype must be built, deployed, and tested. The **same Docker container image** is used for both Lambda and Fargate deployments of each archetype.

---

### 3.1 Archetype 1: Event-Driven API — SeBS Thumbnailer

| Field | Value |
|---|---|
| **Source** | SeBS benchmark suite (Copik et al., ACM Middleware 2021) |
| **GitHub URL** | https://github.com/spcl/serverless-benchmarks (path: `benchmarks/200.multimedia/210.thumbnailer/`) |
| **What It Does** | Receives an image, generates a thumbnail: resize, format conversion (JPEG/PNG/WebP), EXIF metadata extraction |
| **Language** | Python 3.11 with Pillow library |
| **State Management** | Stateless — each invocation is independent |
| **Duration Control** | Vary input image size and operations in the request payload |

**Payload tiers for controlling execution duration:**

| Duration Target | `payload_tier` Value | Image Configuration |
|---|---|---|
| ~500ms | `"small"` | 50KB JPEG, resize to 128×128 only |
| ~2s | `"medium"` | 500KB PNG, resize + format conversion + EXIF extraction |
| ~5s | `"large"` | 2MB TIFF, resize + format conversion + EXIF + watermarking + multi-format compression (9 variants) |

---

### 3.2 Archetype 2: Batch ETL — SeBS-Flow ETL Pipeline

| Field | Value |
|---|---|
| **Source** | SeBS-Flow benchmark suite (Copik et al., arXiv 2024) |
| **GitHub / Reference** | https://arxiv.org/html/2410.03480v2 (ETL pipeline workflow) |
| **What It Does** | Ingests CSV data from S3, performs data cleaning (null handling, type conversion), multi-column aggregations, and exports as Parquet |
| **Language** | Python 3.11 with pandas and pyarrow |
| **State Management** | Stateless — reads from / writes to S3 |
| **Duration Control** | Vary input dataset size: 10K / 100K / 1M rows |

**Payload tiers:**

| Duration Target | `payload_tier` Value | Dataset Configuration |
|---|---|---|
| ~500ms | `"small"` | 10K row CSV, simple single-column aggregation |
| ~2s | `"medium"` | 100K row CSV, multi-column aggregation + type casting + null handling |
| ~5s | `"large"` | 1M row CSV, full pipeline: ingest + clean + aggregate + Parquet export |

---

### 3.3 Archetype 3: ML Inference — SeBS Image-Recognition (ResNet-50)

| Field | Value |
|---|---|
| **Source** | SeBS benchmark suite — `411.image-recognition` |
| **GitHub URL** | https://github.com/spcl/serverless-benchmarks (path: `benchmarks/400.inference/411.image-recognition/`) |
| **What It Does** | Runs image classification using a pre-trained ResNet-50 model (~98MB). Model is loaded on cold start and kept in memory for warm invocations. |
| **Language** | Python 3.11 with PyTorch |
| **State Management** | Stateless (model stays in warm Lambda execution environment) |
| **Duration Control** | Vary batch size (number of images per invocation) |

**Payload tiers:**

| Duration Target | `payload_tier` Value | Batch Configuration |
|---|---|---|
| ~500ms | `"small"` | Batch size = 1 image, MobileNetV2 model (~14MB) |
| ~2s | `"medium"` | Batch size = 4 images, ResNet-50 model (~98MB) |
| ~5s | `"large"` | Batch size = 8 images, ResNet-50 model (~98MB) |

> **IMPORTANT**: The model file must be included in the container image (not downloaded at runtime). This means the standard image variant for this archetype will naturally be larger due to the model weights.

---

### 3.4 Archetype 4: Enterprise Microservice — DeathStarBench Hotel Reservation

| Field | Value |
|---|---|
| **Source** | DeathStarBench (Gan et al., ASPLOS 2019) — Hotel Reservation service |
| **GitHub URL** | https://github.com/delimitrou/DeathStarBench (path: `hotelReservation/`) |
| **What It Does** | Multi-tier hotel booking system: search, recommendation, and full booking endpoints backed by MongoDB, Redis, and Memcached |
| **Language** | Go (original DeathStarBench), wrapped in Python/Flask for Lambda/Fargate deployment |
| **State Management** | **STATEFUL** — requires MongoDB and Redis backends. The only stateful archetype. |
| **Scope Note** | We use a **SIMPLIFIED 2-service slice** (search + booking endpoints) not the full 7+ microservice stack. This keeps deployment tractable while preserving the stateful pattern. |
| **Load Generator** | Uses `wrk2` with Lua scripts — **DIFFERENT** from the Python load generator used for Archetypes 1–3. The Python orchestrator invokes wrk2 as a subprocess. |
| **Duration Control** | Vary operation type: different API endpoints have different downstream call chains |

**Payload tiers:**

| Duration Target | `payload_tier` Value | Operation Configuration |
|---|---|---|
| ~200–500ms | `"search-only"` | `GET /hotels` — MongoDB read + Redis cache lookup |
| ~1–2s | `"search+recommendation"` | `GET /recommendations` — MongoDB read + recommendation service call |
| ~2–5s | `"full-booking"` | `POST /reservation` — search + recommendation + user auth + booking write |

> **NOTE**: Archetype 4 uses `wrk2` with Lua scripts as its load generator. Install wrk2 on the EC2 load generator instance:
> ```bash
> git clone https://github.com/giltene/wrk2 && cd wrk2 && make
> ```
> The Python orchestrator calls wrk2 as a subprocess.

---

### 3.5 Archetype Summary

| # | Name | Source Suite | Language | State | Duration Control | Load Generator |
|---|---|---|---|---|---|---|
| 1 | Event-Driven API (Thumbnailer) | SeBS | Python 3.11 + Pillow | Stateless | Image size / operations | Python (requests) |
| 2 | Batch ETL | SeBS-Flow | Python 3.11 + pandas | Stateless | CSV row count | Python (requests) |
| 3 | ML Inference (ResNet-50) | SeBS | Python 3.11 + PyTorch | Stateless | Batch size | Python (requests) |
| 4 | Enterprise Microservice (Hotel) | DeathStarBench | Go / Python Flask | **Stateful** | API operation type | wrk2 + Lua |
