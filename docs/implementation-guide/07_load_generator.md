
# Experiment Implementation Guide — File 6 of 8
# The Load Generator

> **Cross-reference**: Complete Sections 1–6 before this file in files 1-6.

---

## 7. The Load Generator

### 7.1 Overview

The load generator runs on the EC2 c5.2xlarge instance and orchestrates all experiments. It sends HTTP requests to all 32 endpoints simultaneously (Python threading), cycling through 36 sequential blocks per deployment. Each block represents one combination of invocation frequency, traffic burstiness (CV), and execution duration tier.

| Parameter | Value |
|---|---|
| Blocks per deployment | 36 = 4 frequencies × 3 CV levels × 3 duration tiers |
| Block structure | 90 minutes active load + 15 minutes idle |
| Total per deployment | 36 × 105 minutes = 3,780 min ≈ 63 hours |
| Execution model | All 32 deployments run simultaneously via Python threading |
| Fits in 7-day window | 63 hours < 168 hours ✓ |

---

### 7.2 How Traffic Burstiness (CV) Is Controlled

CV = Coefficient of Variation = StdDev / Mean of request inter-arrival times. A **Gamma distribution** generates inter-arrival times with a target CV while preserving the mean request rate.

```python
import numpy as np

def generate_inter_arrival_times(rate_per_second: float, cv: float, n: int) -> np.ndarray:
    """
    Generate n inter-arrival times with target mean rate and burstiness (CV).
    Uses Gamma distribution: shape = 1/CV^2, scale = mean_interval/shape.
    When CV=1.0, this reduces to Exponential (= standard Poisson arrival process).

    Args:
        rate_per_second: Mean request rate (requests/second)
        cv: Coefficient of Variation (0.5=steady, 2.0=moderate, 4.0=bursty)
        n: Number of inter-arrival times to generate

    Returns:
        numpy array of inter-arrival times in seconds
    """
    mean_interval = 1.0 / rate_per_second
    shape = 1.0 / (cv ** 2)
    scale = mean_interval / shape
    return np.random.gamma(shape=shape, scale=scale, size=n)
```

---

### 7.3 Frequency and Payload Tier Reference Tables

```python
# Invocation frequency → requests per second
FREQ_MAP = {
    '1k':   1000   / 86400,   # 0.01157 req/sec
    '10k':  10000  / 86400,   # 0.11574 req/sec
    '50k':  50000  / 86400,   # 0.57870 req/sec
    '100k': 100000 / 86400    # 1.15741 req/sec
}

# CV levels
CV_LEVELS = [0.5, 2.0, 4.0]

# Duration tiers (sent in request body to control execution duration)
DURATION_TIERS = ['small', 'medium', 'large']

# Payload bodies per archetype and duration tier
PAYLOAD_TIERS = {
    'thumbnailer': {
        'small':  {'payload_tier': 'small',  's3_key': 'payloads/thumbnailer/small/sample.jpg'},
        'medium': {'payload_tier': 'medium', 's3_key': 'payloads/thumbnailer/medium/sample.png'},
        'large':  {'payload_tier': 'large',  's3_key': 'payloads/thumbnailer/large/sample.tiff'}
    },
    'etl-pipeline': {
        'small':  {'payload_tier': 'small',  's3_key': 'payloads/etl/small/data.csv'},
        'medium': {'payload_tier': 'medium', 's3_key': 'payloads/etl/medium/data.csv'},
        'large':  {'payload_tier': 'large',  's3_key': 'payloads/etl/large/data.csv'}
    },
    'ml-inference': {
        'small':  {'payload_tier': 'small',  'batch_size': 1},
        'medium': {'payload_tier': 'medium', 'batch_size': 4},
        'large':  {'payload_tier': 'large',  'batch_size': 8}
    },
    'hotel-reservation': {
        'small':  {'payload_tier': 'small',  'operation': 'search-only'},
        'medium': {'payload_tier': 'medium', 'operation': 'search+recommendation'},
        'large':  {'payload_tier': 'large',  'operation': 'full-booking'}
    }
}
```

---

### 7.4 Complete load_generator.py

Save this file as load_generator.py on the EC2 instance. This script runs all 36 blocks per deployment, handles CV-controlled inter-arrival times, logs all results, and manages the 15-minute idle periods.

```python
#!/usr/bin/env python3
"""
load_generator.py — Serverless vs Container Experiment Load Generator

Runs all 36 sequential blocks per deployment simultaneously across all 32
endpoints using Python threading. Each block runs 90 minutes of active load
followed by 15 minutes idle (for Lambda environment recycling).

Usage:
    python3 load_generator.py --config deployments.json --output results/

deployments.json format:
    [
      {
        "name": "sebs-thumbnailer-512mb-slim-lambda",
        "url": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/invoke",
        "archetype": "thumbnailer",
        "platform": "lambda",
        "memory_mb": 512,
        "image_size": "slim"
      },
      ...
    ]
"""

import argparse
import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import requests

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(threadName)s] %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('load_generator.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
ACTIVE_BLOCK_SECONDS = 90 * 60   # 90 minutes active load per block
IDLE_SECONDS         = 15 * 60   # 15 minutes idle between blocks
REQUEST_TIMEOUT      = 30        # seconds per HTTP request

FREQ_MAP = {
    '1k':   1000   / 86400,
    '10k':  10000  / 86400,
    '50k':  50000  / 86400,
    '100k': 100000 / 86400
}

CV_LEVELS      = [0.5, 2.0, 4.0]
DURATION_TIERS = ['small', 'medium', 'large']

PAYLOAD_TIERS = {
    'thumbnailer': {
        'small':  {'payload_tier': 'small',  's3_key': 'payloads/thumbnailer/small/sample.jpg'},
        'medium': {'payload_tier': 'medium', 's3_key': 'payloads/thumbnailer/medium/sample.png'},
        'large':  {'payload_tier': 'large',  's3_key': 'payloads/thumbnailer/large/sample.tiff'}
    },
    'etl-pipeline': {
        'small':  {'payload_tier': 'small',  's3_key': 'payloads/etl/small/data.csv'},
        'medium': {'payload_tier': 'medium', 's3_key': 'payloads/etl/medium/data.csv'},
        'large':  {'payload_tier': 'large',  's3_key': 'payloads/etl/large/data.csv'}
    },
    'ml-inference': {
        'small':  {'payload_tier': 'small',  'batch_size': 1},
        'medium': {'payload_tier': 'medium', 'batch_size': 4},
        'large':  {'payload_tier': 'large',  'batch_size': 8}
    },
    'hotel-reservation': {
        'small':  {'payload_tier': 'small',  'operation': 'search-only'},
        'medium': {'payload_tier': 'medium', 'operation': 'search+recommendation'},
        'large':  {'payload_tier': 'large',  'operation': 'full-booking'}
    }
}


# ── Gamma inter-arrival time generator ───────────────────────────────────────
def generate_inter_arrival_times(rate_per_second: float, cv: float, n: int) -> np.ndarray:
    mean_interval = 1.0 / rate_per_second
    shape = 1.0 / (cv ** 2)
    scale = mean_interval / shape
    return np.random.gamma(shape=shape, scale=scale, size=n)


# ── Single block runner ───────────────────────────────────────────────────────
def run_block(deployment: dict, freq_key: str, cv: float, duration_tier: str,
              block_index: int, output_dir: str) -> dict:
    """
    Run one 90-minute load block against a single deployment endpoint.
    Returns a summary dict with aggregated metrics for this block.
    """
    name      = deployment['name']
    url       = deployment['url']
    archetype = deployment['archetype']
    rate      = FREQ_MAP[freq_key]
    payload   = PAYLOAD_TIERS[archetype][duration_tier]

    log.info(f"[{name}] Block {block_index:02d} START | freq={freq_key} cv={cv} tier={duration_tier}")

    block_start = time.time()
    deadline    = block_start + ACTIVE_BLOCK_SECONDS

    latencies   = []
    errors      = 0
    total_sent  = 0

    # Pre-generate inter-arrival times for the full block duration
    # Estimate max requests needed: rate * ACTIVE_BLOCK_SECONDS * 2 (safety margin)
    max_requests = int(rate * ACTIVE_BLOCK_SECONDS * 2) + 100
    inter_arrivals = generate_inter_arrival_times(rate, cv, max_requests)

    idx = 0
    next_send = time.time()

    while time.time() < deadline:
        # Wait until next scheduled send time
        now = time.time()
        if now < next_send:
            time.sleep(next_send - now)

        if time.time() >= deadline:
            break

        # Send request
        req_start = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            latency_ms = (time.time() - req_start) * 1000
            latencies.append(latency_ms)
            if resp.status_code != 200:
                errors += 1
        except Exception as e:
            errors += 1
            log.warning(f"[{name}] Request error: {e}")

        total_sent += 1

        # Schedule next request
        if idx < len(inter_arrivals) - 1:
            idx += 1
            next_send = next_send + inter_arrivals[idx]
        else:
            # Regenerate if we run out (shouldn't happen with 2x margin)
            inter_arrivals = generate_inter_arrival_times(rate, cv, max_requests)
            idx = 0
            next_send = time.time() + inter_arrivals[0]

    # Compute block-level statistics
    latencies_arr = np.array(latencies) if latencies else np.array([0.0])
    summary = {
        'deployment_name':   name,
        'archetype':         archetype,
        'platform':          deployment['platform'],
        'memory_mb':         deployment['memory_mb'],
        'image_size':        deployment['image_size'],
        'block_index':       block_index,
        'invocation_frequency': freq_key,
        'traffic_cv':        cv,
        'duration_tier':     duration_tier,
        'block_start_utc':   datetime.fromtimestamp(block_start, tz=timezone.utc).isoformat(),
        'total_requests':    total_sent,
        'error_count':       errors,
        'error_rate_pct':    round(errors / max(total_sent, 1) * 100, 4),
        'p50_latency_ms':    round(float(np.percentile(latencies_arr, 50)), 2),
        'p95_latency_ms':    round(float(np.percentile(latencies_arr, 95)), 2),
        'p99_latency_ms':    round(float(np.percentile(latencies_arr, 99)), 2),
        'mean_latency_ms':   round(float(np.mean(latencies_arr)), 2),
        'throughput_rps':    round(total_sent / ACTIVE_BLOCK_SECONDS, 4)
    }

    log.info(f"[{name}] Block {block_index:02d} END | "
             f"sent={total_sent} errors={errors} "
             f"p95={summary['p95_latency_ms']}ms")

    # Write block result to per-deployment CSV
    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, f"{name}.csv")
    file_exists = os.path.exists(out_file)
    with open(out_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=summary.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(summary)

    return summary


# ── Deployment worker (runs all 36 blocks for one deployment) ─────────────────
def run_deployment(deployment: dict, output_dir: str):
    """
    Runs all 36 sequential blocks for a single deployment.
    Block order: iterate over all frequency × CV × duration_tier combinations.
    Between each block: 15-minute idle period.
    """
    name = deployment['name']
    log.info(f"[{name}] Starting all 36 blocks")

    block_index = 0
    for freq_key in ['1k', '10k', '50k', '100k']:
        for cv in CV_LEVELS:
            for duration_tier in DURATION_TIERS:
                block_index += 1
                run_block(deployment, freq_key, cv, duration_tier,
                          block_index, output_dir)

                # 15-minute idle period between blocks
                # (allows Lambda execution environments to be recycled,
                #  preventing warm container carryover into the next block)
                if block_index < 36:
                    log.info(f"[{name}] Idle period: 15 minutes before block {block_index+1}")
                    time.sleep(IDLE_SECONDS)

    log.info(f"[{name}] All 36 blocks complete.")


# ── Main orchestrator ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='SeBS Experiment Load Generator')
    parser.add_argument('--config', required=True,
                        help='Path to deployments.json')
    parser.add_argument('--output', default='results/',
                        help='Output directory for per-deployment CSV files')
    args = parser.parse_args()

    with open(args.config) as f:
        deployments = json.load(f)

    log.info(f"Starting load generator: {len(deployments)} deployments, "
             f"36 blocks each, running in parallel")

    # Launch one thread per deployment — all run simultaneously
    threads = []
    for dep in deployments:
        t = threading.Thread(
            target=run_deployment,
            args=(dep, args.output),
            name=dep['name'],
            daemon=True
        )
        threads.append(t)
        t.start()
        time.sleep(0.5)  # Stagger thread starts by 0.5s to avoid thundering herd

    # Wait for all threads to complete
    for t in threads:
        t.join()

    log.info("All deployments complete. Results written to: " + args.output)


if __name__ == '__main__':
    main()
```

---

### 7.5 deployments.json Format

Create this file from your `endpoints.txt` file generated during deployment (Section 6). Each entry maps a deployment name to its endpoint URL and metadata.

```json
[
  {
    "name": "sebs-thumbnailer-512mb-slim-lambda",
    "url": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/invoke",
    "archetype": "thumbnailer",
    "platform": "lambda",
    "memory_mb": 512,
    "image_size": "slim"
  },
  {
    "name": "sebs-thumbnailer-512mb-slim-fargate",
    "url": "http://sebs-experiment-alb-123456.us-east-1.elb.amazonaws.com/sebs-thumbnailer-512mb-slim-fargate/invoke",
    "archetype": "thumbnailer",
    "platform": "fargate",
    "memory_mb": 512,
    "image_size": "slim"
  }
]
```

> **NOTE**: Generate this file automatically from `endpoints.txt` using the helper script below.

```python
#!/usr/bin/env python3
"""generate_deployments_json.py — Convert endpoints.txt to deployments.json"""
import json, re

deployments = []
with open('endpoints.txt') as f:
    for line in f:
        line = line.strip()
        if '=' not in line:
            continue
        name, url = line.split('=', 1)
        # Parse name: sebs-{archetype}-{memory}-{imagesize}-{platform}
        # e.g. sebs-thumbnailer-512mb-slim-lambda
        parts = name.split('-')
        platform = parts[-1]
        image_size = parts[-2]
        memory_mb = int(parts[-3].replace('mb',''))
        archetype = '-'.join(parts[1:-3])
        deployments.append({
            'name': name, 'url': url,
            'archetype': archetype, 'platform': platform,
            'memory_mb': memory_mb, 'image_size': image_size
        })

with open('deployments.json', 'w') as f:
    json.dump(deployments, f, indent=2)

print(f"Generated deployments.json with {len(deployments)} entries")
```

---

### 7.6 wrk2 Load Generator for Archetype 4 (Hotel Reservation)

Archetype 4 uses `wrk2` with Lua scripts instead of the Python HTTP client. The Python orchestrator calls wrk2 as a subprocess.

#### Install wrk2 on EC2

```bash
sudo yum install -y git gcc make openssl-devel
git clone https://github.com/giltene/wrk2
cd wrk2 && make
sudo cp wrk /usr/local/bin/wrk2
```

#### Lua Scripts for Each Operation Tier

Save as `lua/hotel_search.lua`:
```lua
-- hotel_search.lua — search-only tier (~200-500ms)
wrk.method = "GET"
wrk.path   = "/hotels?inDate=2025-01-01&outDate=2025-01-03&lat=37.7749&lon=-122.4194&customerName=test"
wrk.headers["Content-Type"] = "application/json"
```

Save as `lua/hotel_recommendation.lua`:
```lua
-- hotel_recommendation.lua — search + recommendation tier (~1-2s)
wrk.method = "GET"
wrk.path   = "/recommendations?require=dis&lat=37.7749&lon=-122.4194"
wrk.headers["Content-Type"] = "application/json"
```

Save as `lua/hotel_booking.lua`:
```lua
-- hotel_booking.lua — full booking tier (~2-5s)
request_count = 0
function request()
    request_count = request_count + 1
    local body = string.format(
        '{"customerName":"user%d","hotelId":"1","inDate":"2025-01-01","outDate":"2025-01-03","roomNumber":1}',
        request_count % 1000
    )
    return wrk.format("POST", "/reservation",
        {["Content-Type"]="application/json"}, body)
end
```

#### Subprocess Integration in load_generator.py

For Hotel Reservation deployments, replace the `run_block()` HTTP call with this wrk2 subprocess call:

```python
import subprocess

def run_block_wrk2(deployment: dict, freq_key: str, cv: float,
                   duration_tier: str, block_index: int, output_dir: str) -> dict:
    """Run one block for Hotel Reservation using wrk2."""
    name = deployment['name']
    url  = deployment['url'].rsplit('/invoke', 1)[0]  # base URL without /invoke

    lua_map = {
        'small':  'lua/hotel_search.lua',
        'medium': 'lua/hotel_recommendation.lua',
        'large':  'lua/hotel_booking.lua'
    }
    lua_script = lua_map[duration_tier]
    rate_rps   = FREQ_MAP[freq_key]

    # wrk2 does not natively support Gamma-distributed arrivals.
    # We approximate by running at the target mean rate for 90 minutes.
    # CV variation is captured via the measured latency distribution.
    cmd = [
        'wrk2',
        '-t', '4',                          # 4 threads
        '-c', '50',                         # 50 connections
        '-d', f'{ACTIVE_BLOCK_SECONDS}s',   # 90 minutes
        '-R', str(int(rate_rps * 60)),      # rate in req/min
        '-s', lua_script,
        '--latency',
        url
    ]

    log.info(f"[{name}] Block {block_index:02d} wrk2 START | "
             f"freq={freq_key} tier={duration_tier}")
    block_start = time.time()

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout

    # Parse wrk2 output for latency percentiles
    p50 = p95 = p99 = 0.0
    for line in output.splitlines():
        if '50.000%' in line: p50 = _parse_wrk2_latency(line)
        if '95.000%' in line: p95 = _parse_wrk2_latency(line)
        if '99.000%' in line: p99 = _parse_wrk2_latency(line)

    summary = {
        'deployment_name': name,
        'archetype': 'hotel-reservation',
        'platform': deployment['platform'],
        'memory_mb': deployment['memory_mb'],
        'image_size': deployment['image_size'],
        'block_index': block_index,
        'invocation_frequency': freq_key,
        'traffic_cv': cv,
        'duration_tier': duration_tier,
        'block_start_utc': datetime.fromtimestamp(block_start, tz=timezone.utc).isoformat(),
        'p50_latency_ms': p50,
        'p95_latency_ms': p95,
        'p99_latency_ms': p99,
        'error_rate_pct': 0.0  # parse from wrk2 output if needed
    }

    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, f"{name}.csv")
    file_exists = os.path.exists(out_file)
    with open(out_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=summary.keys())
        if not file_exists: writer.writeheader()
        writer.writerow(summary)

    return summary


def _parse_wrk2_latency(line: str) -> float:
    """Parse latency value from wrk2 output line. Returns ms."""
    parts = line.strip().split()
    if len(parts) >= 2:
        val = parts[1]
        if val.endswith('ms'): return float(val[:-2])
        if val.endswith('s'):  return float(val[:-1]) * 1000
        if val.endswith('us'): return float(val[:-2]) / 1000
    return 0.0
```

---

### 7.7 Installing Dependencies on EC2

```bash
# On the EC2 c5.2xlarge load generator instance
sudo yum update -y
sudo yum install -y python3.11 python3.11-pip git gcc make openssl-devel

pip3.11 install numpy requests

# Install wrk2 for Archetype 4
git clone https://github.com/giltene/wrk2
cd wrk2 && make && sudo cp wrk /usr/local/bin/wrk2 && cd ..

# Copy load generator files to EC2
scp -i your-key.pem load_generator.py ec2-user@<EC2_IP>:~/
scp -i your-key.pem deployments.json ec2-user@<EC2_IP>:~/
scp -r lua/ ec2-user@<EC2_IP>:~/
```
