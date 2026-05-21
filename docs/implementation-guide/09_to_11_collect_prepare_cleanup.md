
# Experiment Implementation Guide — File 8 of 8
# Data Collection, Training Data Prep, Cleanup

> **Cross-reference**: Complete Sections 1–8 before this file. Run all collection scripts only after the load generator has completed all 36 blocks per deployment.

---

## 9. Collecting and Aggregating Results

### 9.1 Overview of What to Collect

After the load generator completes, you will collect three categories of data and merge them into a single `results.csv` file with 1,152 rows (32 deployments × 36 blocks).

| Category | Source | Metrics |
|---|---|---|
| **Performance** | CloudWatch Metrics + load generator CSVs | p50/p95/p99 latency, throughput, error rate |
| **Cold Start** | CloudWatch Logs Insights (Lambda only) | Cold start frequency (%), cold start duration (ms) |
| **Cost (TCO)** | AWS Cost Explorer API | Compute cost, API Gateway / ALB cost, CloudWatch Logs ingestion cost |

> **IMPORTANT — Cost Scope**: Include only platform-attributable costs. **Include**: Lambda GB-second compute, Fargate vCPU-hour + GB-hour compute, API Gateway per-request charges (Lambda), ALB fixed hourly + LCU charges (Fargate), CloudWatch Logs ingestion. **Exclude**: S3 storage, data transfer costs (these are identical for both platforms and would not affect the comparison).

---

### 9.2 Cold Start Extraction from CloudWatch Logs Insights

Lambda cold starts are identified by the presence of an `Init Duration` field in the CloudWatch Logs REPORT line. This field only appears when a cold start occurred.

#### CloudWatch Logs Insights Query

Run this query for each Lambda function, scoped to the time window of each block:

```
fields @timestamp, @requestId, @duration, @billedDuration, @initDuration
| filter @type = "REPORT"
| stats
    count(*) as total_invocations,
    count(@initDuration) as cold_start_count,
    avg(@initDuration) as avg_cold_start_ms,
    pct(@initDuration, 95) as p95_cold_start_ms,
    avg(@duration) as avg_duration_ms,
    pct(@duration, 95) as p95_duration_ms
  by bin(90m)
```

#### Python Script to Run the Query Programmatically

```python
#!/usr/bin/env python3
"""
query_cold_starts.py — Extract cold start metrics from CloudWatch Logs Insights.
Run after the experiment completes.
Usage: python3 query_cold_starts.py --start-time 2026-04-14T00:00:00Z \
                                     --end-time   2026-04-21T00:00:00Z \
                                     --output     cold_starts.csv
"""
import argparse
import csv
import json
import time
from datetime import datetime, timezone

import boto3

logs = boto3.client('logs', region_name='us-east-1')

LAMBDA_FUNCTIONS = [
    'sebs-thumbnailer-512mb-slim-lambda',
    'sebs-thumbnailer-512mb-standard-lambda',
    'sebs-thumbnailer-2gb-slim-lambda',
    'sebs-thumbnailer-2gb-standard-lambda',
    'sebs-etl-pipeline-512mb-slim-lambda',
    'sebs-etl-pipeline-512mb-standard-lambda',
    'sebs-etl-pipeline-2gb-slim-lambda',
    'sebs-etl-pipeline-2gb-standard-lambda',
    'sebs-ml-inference-512mb-slim-lambda',
    'sebs-ml-inference-512mb-standard-lambda',
    'sebs-ml-inference-2gb-slim-lambda',
    'sebs-ml-inference-2gb-standard-lambda',
    'sebs-hotel-512mb-slim-lambda',
    'sebs-hotel-512mb-standard-lambda',
    'sebs-hotel-2gb-slim-lambda',
    'sebs-hotel-2gb-standard-lambda',
]

QUERY = """
fields @timestamp, @requestId, @duration, @billedDuration, @initDuration
| filter @type = "REPORT"
| stats
    count(*) as total_invocations,
    count(@initDuration) as cold_start_count,
    avg(@initDuration) as avg_cold_start_ms,
    pct(@initDuration, 95) as p95_cold_start_ms,
    avg(@duration) as avg_duration_ms,
    pct(@duration, 95) as p95_duration_ms
  by bin(90m)
"""


def run_query(log_group: str, start_time: datetime, end_time: datetime) -> list:
    """Run a CloudWatch Logs Insights query and return results."""
    start_ts = int(start_time.timestamp())
    end_ts   = int(end_time.timestamp())

    response = logs.start_query(
        logGroupName=log_group,
        startTime=start_ts,
        endTime=end_ts,
        queryString=QUERY
    )
    query_id = response['queryId']

    # Poll until complete
    while True:
        result = logs.get_query_results(queryId=query_id)
        status = result['status']
        if status == 'Complete':
            return result['results']
        elif status in ('Failed', 'Cancelled', 'Timeout'):
            print(f"  Query {status} for {log_group}")
            return []
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-time', required=True,
                        help='Experiment start time in ISO format (UTC)')
    parser.add_argument('--end-time', required=True,
                        help='Experiment end time in ISO format (UTC)')
    parser.add_argument('--output', default='cold_starts.csv')
    args = parser.parse_args()

    start_dt = datetime.fromisoformat(args.start_time.replace('Z', '+00:00'))
    end_dt   = datetime.fromisoformat(args.end_time.replace('Z', '+00:00'))

    rows = []
    for func_name in LAMBDA_FUNCTIONS:
        log_group = f'/aws/lambda/{func_name}'
        print(f"Querying: {log_group}")
        results = run_query(log_group, start_dt, end_dt)

        for record in results:
            row = {'function_name': func_name}
            for field in record:
                row[field['field']] = field['value']
            rows.append(row)

    if rows:
        with open(args.output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"
Cold start data written to {args.output} ({len(rows)} records)")
    else:
        print("No results returned.")


if __name__ == '__main__':
    main()
```

---

### 9.3 Latency Percentiles from CloudWatch Metrics

```python
#!/usr/bin/env python3
"""
query_latency.py — Pull p50/p95/p99 latency from CloudWatch Metrics.
For Lambda: uses AWS/Lambda Duration metric.
For Fargate: uses AWS/ApplicationELB TargetResponseTime metric.
Usage: python3 query_latency.py --start-time 2026-04-14T00:00:00Z \
                                 --end-time   2026-04-21T00:00:00Z \
                                 --output     latency_metrics.csv
"""
import argparse
import csv
from datetime import datetime

import boto3

cw = boto3.client('cloudwatch', region_name='us-east-1')

LAMBDA_FUNCTIONS = [
    'sebs-thumbnailer-512mb-slim-lambda',
    'sebs-thumbnailer-512mb-standard-lambda',
    'sebs-thumbnailer-2gb-slim-lambda',
    'sebs-thumbnailer-2gb-standard-lambda',
    'sebs-etl-pipeline-512mb-slim-lambda',
    'sebs-etl-pipeline-512mb-standard-lambda',
    'sebs-etl-pipeline-2gb-slim-lambda',
    'sebs-etl-pipeline-2gb-standard-lambda',
    'sebs-ml-inference-512mb-slim-lambda',
    'sebs-ml-inference-512mb-standard-lambda',
    'sebs-ml-inference-2gb-slim-lambda',
    'sebs-ml-inference-2gb-standard-lambda',
    'sebs-hotel-512mb-slim-lambda',
    'sebs-hotel-512mb-standard-lambda',
    'sebs-hotel-2gb-slim-lambda',
    'sebs-hotel-2gb-standard-lambda',
]

FARGATE_SERVICES = [
    'sebs-thumbnailer-512mb-slim-fargate',
    'sebs-thumbnailer-512mb-standard-fargate',
    'sebs-thumbnailer-2gb-slim-fargate',
    'sebs-thumbnailer-2gb-standard-fargate',
    'sebs-etl-pipeline-512mb-slim-fargate',
    'sebs-etl-pipeline-512mb-standard-fargate',
    'sebs-etl-pipeline-2gb-slim-fargate',
    'sebs-etl-pipeline-2gb-standard-fargate',
    'sebs-ml-inference-512mb-slim-fargate',
    'sebs-ml-inference-512mb-standard-fargate',
    'sebs-ml-inference-2gb-slim-fargate',
    'sebs-ml-inference-2gb-standard-fargate',
    'sebs-hotel-512mb-slim-fargate',
    'sebs-hotel-512mb-standard-fargate',
    'sebs-hotel-2gb-slim-fargate',
    'sebs-hotel-2gb-standard-fargate',
]


def get_lambda_latency(func_name: str, start: datetime, end: datetime) -> dict:
    """Get p50/p95/p99 latency for a Lambda function over the experiment window."""
    result = {'function': func_name, 'platform': 'lambda'}
    for stat, label in [('p50', 'p50'), ('p95', 'p95'), ('p99', 'p99')]:
        resp = cw.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName='Duration',
            Dimensions=[{'Name': 'FunctionName', 'Value': func_name}],
            StartTime=start,
            EndTime=end,
            Period=int((end - start).total_seconds()),
            ExtendedStatistics=[f'p{stat[1:]}']
        )
        if resp['Datapoints']:
            result[f'{label}_ms'] = round(
                resp['Datapoints'][0]['ExtendedStatistics'][f'p{stat[1:]}'], 2)
        else:
            result[f'{label}_ms'] = None
    return result


def get_fargate_latency(svc_name: str, alb_arn_suffix: str,
                        tg_arn_suffix: str, start: datetime, end: datetime) -> dict:
    """Get p50/p95/p99 latency for a Fargate service via ALB TargetResponseTime."""
    result = {'function': svc_name, 'platform': 'fargate'}
    for stat, label in [('p50', 'p50'), ('p95', 'p95'), ('p99', 'p99')]:
        resp = cw.get_metric_statistics(
            Namespace='AWS/ApplicationELB',
            MetricName='TargetResponseTime',
            Dimensions=[
                {'Name': 'LoadBalancer', 'Value': alb_arn_suffix},
                {'Name': 'TargetGroup',  'Value': tg_arn_suffix}
            ],
            StartTime=start,
            EndTime=end,
            Period=int((end - start).total_seconds()),
            ExtendedStatistics=[f'p{stat[1:]}']
        )
        if resp['Datapoints']:
            # ALB reports in seconds — convert to ms
            result[f'{label}_ms'] = round(
                resp['Datapoints'][0]['ExtendedStatistics'][f'p{stat[1:]}'] * 1000, 2)
        else:
            result[f'{label}_ms'] = None
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-time', required=True)
    parser.add_argument('--end-time',   required=True)
    parser.add_argument('--alb-arn-suffix', required=True,
                        help='ALB ARN suffix for CloudWatch dimension '
                             '(e.g. app/sebs-experiment-alb/abc123)')
    parser.add_argument('--output', default='latency_metrics.csv')
    args = parser.parse_args()

    start_dt = datetime.fromisoformat(args.start_time.replace('Z', '+00:00'))
    end_dt   = datetime.fromisoformat(args.end_time.replace('Z', '+00:00'))

    rows = []
    for func in LAMBDA_FUNCTIONS:
        print(f"Lambda latency: {func}")
        rows.append(get_lambda_latency(func, start_dt, end_dt))

    # NOTE: For Fargate, you need the ALB ARN suffix and target group ARN suffix.
    # Retrieve these from the AWS Console or from the deployment scripts.
    # Example: alb_arn_suffix = "app/sebs-experiment-alb/0123456789abcdef"
    #          tg_arn_suffix  = "targetgroup/sebs-thumbnailer-512mb-slim/abcdef123456"
    # Populate the loop below with actual values from your deployment.
    print("NOTE: Fargate latency requires ALB/TG ARN suffixes — populate manually.")

    with open(args.output, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"
Latency metrics written to {args.output}")


if __name__ == '__main__':
    main()
```

---

### 9.4 Cost Data from AWS Cost Explorer

```python
#!/usr/bin/env python3
"""
query_costs.py — Pull per-resource cost data from AWS Cost Explorer.
Covers: Lambda compute, Fargate compute, API Gateway, ALB, CloudWatch Logs.
Usage: python3 query_costs.py --start-date 2026-04-14 \
                               --end-date   2026-04-22 \
                               --output     costs.csv
NOTE: Cost Explorer data has a ~24-hour lag. Run this script the day after
      the experiment completes to ensure all charges are visible.
"""
import argparse
import csv

import boto3

ce = boto3.client('ce', region_name='us-east-1')

SERVICES_TO_QUERY = [
    'AWS Lambda',
    'Amazon Elastic Container Service',
    'Amazon API Gateway',
    'AWS Elastic Load Balancing',
    'AmazonCloudWatch',
]


def get_costs_by_service(start_date: str, end_date: str) -> list:
    """Get daily costs grouped by service and usage type."""
    response = ce.get_cost_and_usage(
        TimePeriod={'Start': start_date, 'End': end_date},
        Granularity='DAILY',
        Filter={
            'Dimensions': {
                'Key': 'SERVICE',
                'Values': SERVICES_TO_QUERY
            }
        },
        GroupBy=[
            {'Type': 'DIMENSION', 'Key': 'SERVICE'},
            {'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'}
        ],
        Metrics=['UnblendedCost']
    )

    rows = []
    for period in response['ResultsByTime']:
        date = period['TimePeriod']['Start']
        for group in period['Groups']:
            service    = group['Keys'][0]
            usage_type = group['Keys'][1]
            amount     = float(group['Metrics']['UnblendedCost']['Amount'])
            unit       = group['Metrics']['UnblendedCost']['Unit']
            if amount > 0:
                rows.append({
                    'date':       date,
                    'service':    service,
                    'usage_type': usage_type,
                    'cost_usd':   round(amount, 6),
                    'unit':       unit
                })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-date', required=True, help='YYYY-MM-DD')
    parser.add_argument('--end-date',   required=True, help='YYYY-MM-DD (exclusive)')
    parser.add_argument('--output', default='costs.csv')
    args = parser.parse_args()

    print(f"Querying Cost Explorer: {args.start_date} to {args.end_date}")
    rows = get_costs_by_service(args.start_date, args.end_date)

    with open(args.output, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    total = sum(r['cost_usd'] for r in rows)
    print(f"Total experiment cost: ${total:.2f}")
    print(f"Cost breakdown written to {args.output} ({len(rows)} line items)")


if __name__ == '__main__':
    main()
```

---

### 9.5 Aggregating All Data into results.csv

This script merges the load generator per-deployment CSVs, cold start data, latency metrics, and cost data into a single `results.csv` with 1,152 rows.

```python
#!/usr/bin/env python3
"""
aggregate_results.py — Merge all data sources into a single results.csv.
Produces 1,152 block-level records (32 deployments × 36 blocks).

Usage:
    python3 aggregate_results.py \
        --load-gen-dir  results/ \
        --cold-starts   cold_starts.csv \
        --latency       latency_metrics.csv \
        --costs         costs.csv \
        --output        results.csv
"""
import argparse
import csv
import os
from collections import defaultdict

import pandas as pd


def load_load_gen_results(results_dir: str) -> pd.DataFrame:
    """Load all per-deployment CSV files from the load generator output."""
    frames = []
    for fname in os.listdir(results_dir):
        if fname.endswith('.csv'):
            df = pd.read_csv(os.path.join(results_dir, fname))
            frames.append(df)
    if not frames:
        raise ValueError(f"No CSV files found in {results_dir}")
    combined = pd.concat(frames, ignore_index=True)
    print(f"Load generator records: {len(combined)}")
    return combined


def load_cold_starts(cold_starts_path: str) -> pd.DataFrame:
    """Load cold start data from CloudWatch Logs Insights query output."""
    df = pd.read_csv(cold_starts_path)
    # Rename columns to match results schema
    df = df.rename(columns={
        'function_name':    'deployment_name',
        'cold_start_count': 'cold_start_count',
        'total_invocations':'total_invocations_cw',
        'avg_cold_start_ms':'cold_start_duration_ms',
        'p95_duration_ms':  'cw_p95_duration_ms'
    })
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--load-gen-dir', required=True)
    parser.add_argument('--cold-starts',  required=True)
    parser.add_argument('--latency',      required=True)
    parser.add_argument('--costs',        required=True)
    parser.add_argument('--output',       default='results.csv')
    args = parser.parse_args()

    # 1. Load base data from load generator
    base = load_load_gen_results(args.load_gen_dir)

    # 2. Load cold start data (Lambda only — Fargate cold_start_rate = 0)
    cold = load_cold_starts(args.cold_starts)

    # 3. Merge cold start data onto base
    base = base.merge(
        cold[['deployment_name', 'cold_start_count',
              'total_invocations_cw', 'cold_start_duration_ms']],
        on='deployment_name', how='left'
    )

    # 4. Compute cold start rate (%)
    base['cold_start_rate_pct'] = (
        base['cold_start_count'].fillna(0) /
        base['total_requests'].replace(0, 1) * 100
    ).round(4)
    base['cold_start_duration_ms'] = base['cold_start_duration_ms'].fillna(0)

    # 5. For Fargate deployments, cold start metrics are 0
    fargate_mask = base['platform'] == 'fargate'
    base.loc[fargate_mask, 'cold_start_rate_pct']    = 0.0
    base.loc[fargate_mask, 'cold_start_duration_ms'] = 0.0

    # 6. Add image_size_mb column (slim=50, standard=250)
    base['image_size_mb'] = base['image_size'].map({'slim': 50, 'standard': 250})

    # 7. Add state_management column (hotel-reservation=1, others=0)
    base['state_management'] = (
        base['archetype'] == 'hotel-reservation').astype(int)

    # 8. Select and order final columns
    output_cols = [
        'deployment_name',
        'archetype',
        'platform',
        'memory_mb',
        'image_size_mb',
        'state_management',
        'invocation_frequency',
        'traffic_cv',
        'duration_tier',
        'block_start_utc',
        'total_requests',
        'error_rate_pct',
        'p50_latency_ms',
        'p95_latency_ms',
        'p99_latency_ms',
        'mean_latency_ms',
        'throughput_rps',
        'cold_start_rate_pct',
        'cold_start_duration_ms',
    ]

    # Keep only columns that exist
    output_cols = [c for c in output_cols if c in base.columns]
    result = base[output_cols]

    result.to_csv(args.output, index=False)
    print(f"
Final results.csv: {len(result)} records")
    print(f"Expected:          1,152 records (32 deployments × 36 blocks)")
    if len(result) != 1152:
        print(f"WARNING: Record count mismatch. Check for missing blocks.")
    print(f"Output written to: {args.output}")


if __name__ == '__main__':
    main()
```

---

### 9.6 Output CSV Schema (results.csv)

The final `results.csv` has the following columns. This is the file handed to the regression model training scripts.

| Column | Type | Description |
|---|---|---|
| `deployment_name` | string | Full deployment name (e.g. `sebs-thumbnailer-512mb-slim-lambda`) |
| `archetype` | string | One of: `thumbnailer`, `etl-pipeline`, `ml-inference`, `hotel-reservation` |
| `platform` | string | `lambda` or `fargate` |
| `memory_mb` | int | 512 or 2048 |
| `image_size_mb` | int | 50 (slim) or 250 (standard) |
| `state_management` | int | 0 = stateless, 1 = stateful |
| `invocation_frequency` | string | `1k`, `10k`, `50k`, or `100k` |
| `traffic_cv` | float | 0.5, 2.0, or 4.0 |
| `duration_tier` | string | `small`, `medium`, or `large` |
| `block_start_utc` | ISO datetime | UTC timestamp when the block started |
| `total_requests` | int | Total requests sent during the 90-minute block |
| `error_rate_pct` | float | Percentage of requests that returned non-200 |
| `p50_latency_ms` | float | 50th percentile end-to-end latency (ms) |
| `p95_latency_ms` | float | 95th percentile end-to-end latency (ms) — primary performance metric |
| `p99_latency_ms` | float | 99th percentile end-to-end latency (ms) |
| `mean_latency_ms` | float | Mean end-to-end latency (ms) |
| `throughput_rps` | float | Measured throughput (requests/second) |
| `cold_start_rate_pct` | float | % of invocations with a cold start (Lambda only; 0 for Fargate) |
| `cold_start_duration_ms` | float | Average cold start Init Duration (ms) from CloudWatch Logs |

---

## 10. Preparing Data for Regression Model Training

### 10.1 Train/Test Split

```python
#!/usr/bin/env python3
"""
prepare_training_data.py — Split results.csv into train and test sets.
Stratified by archetype to ensure all four archetypes are represented
proportionally in both sets.

Usage: python3 prepare_training_data.py --input results.csv --output-dir training/
"""
import argparse
import os

import pandas as pd
from sklearn.model_selection import train_test_split

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',      default='results.csv')
    parser.add_argument('--output-dir', default='training/')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    df = pd.read_csv(args.input)
    print(f"Total records: {len(df)}")

    # Stratified split by archetype
    train, test = train_test_split(
        df,
        test_size=0.20,
        random_state=42,
        stratify=df['archetype']
    )

    train.to_csv(os.path.join(args.output_dir, 'train.csv'), index=False)
    test.to_csv(os.path.join(args.output_dir,  'test.csv'),  index=False)

    print(f"Training set: {len(train)} records (80%)")
    print(f"Test set:     {len(test)}  records (20%)")
    print(f"
Archetype distribution in training set:")
    print(train['archetype'].value_counts())
    print(f"
Archetype distribution in test set:")
    print(test['archetype'].value_counts())
    print(f"
Files written to: {args.output_dir}")

if __name__ == '__main__':
    main()
```

---

### 10.2 Feature Engineering

The following derived features must be added before training. These are required by the regression models as described in the paper.

| Derived Feature | Formula | Used By |
|---|---|---|
| `log_invocations` | `log(invocation_frequency_numeric)` | Cold start model (Model 3) |
| `invocation_frequency_numeric` | Map: 1k→1000, 10k→10000, 50k→50000, 100k→100000 | All models |
| `duration_tier_numeric` | Map: small→500, medium→2000, large→5000 | Cost and latency models |
| `sustained_load` | `invocation_frequency_numeric × duration_tier_numeric / 1e6` | Fargate cost model (Model 2) |
| `image_size_mb` | Already in results.csv (50 or 250) | Cold start model (Model 3) |

```python
import numpy as np
import pandas as pd

def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived features required by regression models."""

    freq_map = {'1k': 1000, '10k': 10000, '50k': 50000, '100k': 100000}
    dur_map  = {'small': 500, 'medium': 2000, 'large': 5000}

    df['invocation_frequency_numeric'] = df['invocation_frequency'].map(freq_map)
    df['duration_tier_numeric']        = df['duration_tier'].map(dur_map)
    df['log_invocations']              = np.log(df['invocation_frequency_numeric'])
    df['sustained_load']               = (
        df['invocation_frequency_numeric'] * df['duration_tier_numeric'] / 1e6
    )
    return df
```

---

### 10.3 Expected Record Counts After Split

| Set | Records | Notes |
|---|---|---|
| Total | 1,152 | 32 deployments × 36 blocks |
| Training | 922 | 80%, stratified by archetype |
| Test | 230 | 20%, held out for final model evaluation |
| Per archetype (train) | ~230 | 8 deployments × 36 blocks × 0.8 |
| Per archetype (test) | ~58 | 8 deployments × 36 blocks × 0.2 |

---

## 11. Cleanup

> **WARNING**: Run cleanup only after you have confirmed `results.csv` is complete and backed up. All resources will be permanently deleted.

### 11.1 Estimated Experiment Cost

| Resource | Estimated Cost (2-week experiment) |
|---|---|
| Lambda compute (16 functions × 7 days) | $40–80 |
| Fargate compute (16 services × 7 days) | $120–200 |
| API Gateway (HTTP API, per-request) | $10–20 |
| ALB (fixed hourly + LCU) | $25–40 |
| NAT Gateway (hourly + data) | $20–30 |
| CloudWatch Logs ingestion | $15–30 |
| EC2 c5.2xlarge (load generator, 9 days) | $30–50 |
| ECR storage (16 images) | $5–10 |
| S3 storage + requests | $5–10 |
| **Total estimate** | **$270–470** |

---

### 11.2 Cleanup Script

```bash
#!/bin/bash
# cleanup.sh — Delete all experiment resources to stop AWS charges.
# Run ONLY after results.csv is confirmed complete and backed up.

source experiment-env.sh
 
# Delete all Lambda functions (run for each of the 16 Lambda deployments)
for FUNC in $(aws lambda list-functions --query 'Functions[?starts_with(FunctionName, `sebs-`)].FunctionName' --output text); do
  echo "Deleting Lambda: $FUNC"
  aws lambda delete-function --function-name $FUNC
done
 
# Delete all API Gateways created for Lambda functions
for API in $(aws apigatewayv2 get-apis --query 'Items[?starts_with(Name, `sebs-`)].ApiId' --output text); do
  echo "Deleting API Gateway: $API"
  aws apigatewayv2 delete-api --api-id $API
done
 
# Scale down and delete all ECS services
for SVC in $(aws ecs list-services --cluster sebs-experiment-cluster --query 'serviceArns[]' --output text); do
  SVC_NAME=$(basename $SVC)
  echo "Deleting ECS service: $SVC_NAME"
  aws ecs update-service --cluster sebs-experiment-cluster --service $SVC_NAME --desired-count 0
  aws ecs delete-service --cluster sebs-experiment-cluster --service $SVC_NAME
done
 
# Deregister task definitions
for TD in $(aws ecs list-task-definitions --family-prefix sebs --query 'taskDefinitionArns[]' --output text); do
  aws ecs deregister-task-definition --task-definition $TD
done
 
# Delete ALB listener rules and target groups
for TG in $(aws elbv2 describe-target-groups --query 'TargetGroups[?starts_with(TargetGroupName, `sebs-`)].TargetGroupArn' --output text); do
  echo "Deleting target group: $TG"
  aws elbv2 delete-target-group --target-group-arn $TG
done
 
# Delete ALB
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN
 
# Delete ECR images
for ARCHETYPE in thumbnailer etl-pipeline ml-inference hotel-reservation; do
  aws ecr delete-repository --repository-name sebs-experiment/${ARCHETYPE} --force
done
 
# Empty and delete S3 bucket
aws s3 rm s3://${BUCKET_NAME} --recursive
aws s3 rb s3://${BUCKET_NAME}
 
# Delete NAT Gateway (charges stop after deletion)
aws ec2 delete-nat-gateway --nat-gateway-id $NAT_GW
# Wait for NAT Gateway to delete, then release Elastic IP
aws ec2 wait nat-gateway-deleted --nat-gateway-ids $NAT_GW || sleep 120
aws ec2 release-address --allocation-id $EIP_ALLOC
 
# Delete VPC resources
aws ec2 detach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID
aws ec2 delete-internet-gateway --internet-gateway-id $IGW_ID
aws ec2 delete-security-group --group-id $SG_ID
# Delete subnets, route tables, then VPC
for SUBNET in $SUBNET_PUB_A $SUBNET_PUB_B $SUBNET_PRIV_A $SUBNET_PRIV_B; do
  aws ec2 delete-subnet --subnet-id $SUBNET
done
aws ec2 delete-route-table --route-table-id $PUBLIC_RT
aws ec2 delete-route-table --route-table-id $PRIVATE_RT
aws ec2 delete-vpc --vpc-id $VPC_ID
 
# Delete IAM roles
for POLICY in AWSLambdaBasicExecutionRole AWSLambdaVPCAccessExecutionRole AmazonS3FullAccess AWSXRayDaemonWriteAccess; do
  aws iam detach-role-policy --role-name sebs-lambda-execution-role \
    --policy-arn arn:aws:iam::aws:policy/${POLICY} 2>/dev/null || true
  aws iam detach-role-policy --role-name sebs-fargate-execution-role \
    --policy-arn arn:aws:iam::aws:policy/${POLICY} 2>/dev/null || true
done
aws iam detach-role-policy --role-name sebs-fargate-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name sebs-lambda-execution-role
aws iam delete-role --role-name sebs-fargate-execution-role
 
echo "Cleanup complete. Verify no resources remain in AWS Console."
```
