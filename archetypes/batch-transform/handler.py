"""
Batch Transform handler (Archetype 2: ETL Pipeline).

Based on the SeBS-Flow ETL pipeline pattern (Copik et al., arXiv 2410.03480v2).
Downloads CSV from S3, performs tiered data processing (cleaning, aggregation,
optional Parquet export), and returns results.

Dual-entrypoint: Lambda handler() + Fargate Flask server on port 8080.
"""

import json
import time
import io
import os

import boto3
import pandas as pd

s3 = boto3.client("s3")
BUCKET = os.environ.get("DATA_BUCKET", "svc-experiment-data")
PLATFORM = os.environ.get("PLATFORM", "lambda")


def process(payload_tier, s3_key):
    """Core ETL processing logic — identical for Lambda and Fargate."""
    start = time.time()

    # Download CSV from S3
    resp = s3.get_object(Bucket=BUCKET, Key=s3_key)
    csv_bytes = resp["Body"].read()
    df = pd.read_csv(io.BytesIO(csv_bytes))

    row_count = len(df)
    aggregation_results = {}

    if payload_tier == "small":
        # Small: 10K rows, single-column aggregation
        # Group by category, sum value_a
        agg = df.groupby("category")["value_a"].sum()
        aggregation_results = agg.to_dict()

    elif payload_tier == "medium":
        # Medium: 100K rows, multi-column aggregation + type casting + null handling
        # Type casting
        df["value_a"] = pd.to_numeric(df["value_a"], errors="coerce")
        df["value_b"] = pd.to_numeric(df["value_b"], errors="coerce")

        # Null handling — fill numeric nulls with column median
        df["value_a"] = df["value_a"].fillna(df["value_a"].median())
        df["value_b"] = df["value_b"].fillna(df["value_b"].median())
        df["category"] = df["category"].fillna("UNKNOWN")
        df["region"] = df["region"].fillna("UNKNOWN")
        df["status"] = df["status"].fillna("UNKNOWN")

        # Multi-column aggregation: group by category + region
        agg = (
            df.groupby(["category", "region"])
            .agg(
                sum_value_a=("value_a", "sum"),
                mean_value_b=("value_b", "mean"),
                count=("id", "count"),
            )
            .reset_index()
        )
        aggregation_results = agg.to_dict(orient="records")

    elif payload_tier == "large":
        # Large: 1M rows, full pipeline — ingest + clean + aggregate + Parquet export

        # --- Clean ---
        df["value_a"] = pd.to_numeric(df["value_a"], errors="coerce")
        df["value_b"] = pd.to_numeric(df["value_b"], errors="coerce")
        df["value_a"] = df["value_a"].fillna(df["value_a"].median())
        df["value_b"] = df["value_b"].fillna(df["value_b"].median())
        df["category"] = df["category"].fillna("UNKNOWN")
        df["region"] = df["region"].fillna("UNKNOWN")
        df["status"] = df["status"].fillna("UNKNOWN")

        # Filter out inactive rows for aggregation
        active_df = df[df["status"] != "inactive"]

        # --- Aggregate ---
        agg = (
            active_df.groupby(["category", "region", "status"])
            .agg(
                sum_value_a=("value_a", "sum"),
                mean_value_a=("value_a", "mean"),
                sum_value_b=("value_b", "sum"),
                mean_value_b=("value_b", "mean"),
                count=("id", "count"),
            )
            .reset_index()
        )
        aggregation_results = {
            "groups": len(agg),
            "total_active_rows": int(active_df.shape[0]),
            "summary": agg.describe().to_dict(),
        }

        # --- Parquet export to S3 ---
        parquet_buffer = io.BytesIO()
        agg.to_parquet(parquet_buffer, engine="pyarrow", index=False)
        parquet_buffer.seek(0)
        parquet_key = s3_key.rsplit("/", 1)[0] + "/aggregated.parquet"
        s3.put_object(Bucket=BUCKET, Key=parquet_key, Body=parquet_buffer.getvalue())
        aggregation_results["parquet_key"] = parquet_key

    execution_ms = round((time.time() - start) * 1000, 2)
    return {
        "payload_tier": payload_tier,
        "execution_ms": execution_ms,
        "row_count": row_count,
        "aggregation_results": aggregation_results,
    }


# --------------- Lambda entrypoint ---------------
def handler(event, context=None):
    """AWS Lambda handler function."""
    try:
        tier = event.get("payload_tier", "small")
        key = event.get("s3_key", f"payloads/batch-transform/{tier}/data.csv")
        result = process(tier, key)
        return {"statusCode": 200, "body": json.dumps(result, default=str)}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


# --------------- Fargate entrypoint (Flask) ---------------
if PLATFORM == "fargate":
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/invoke", methods=["POST"])
    @app.route("/<path:_prefix>/invoke", methods=["POST"])
    def invoke(_prefix=None):
        ev = request.get_json()
        r = handler(ev)
        return jsonify(json.loads(r["body"])), r["statusCode"]

    @app.route("/health", methods=["GET"])
    @app.route("/<path:_prefix>/health", methods=["GET"])
    def health(_prefix=None):
        return jsonify({"status": "healthy"}), 200

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=8080)
