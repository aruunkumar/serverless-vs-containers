#!/usr/bin/env python3
"""Prepare model-ready train/test CSVs for the serverless vs container experiment.

Two input modes:
  1. Consume results.csv from aggregate_results.py (full pipeline, production)
  2. Consume raw per-deployment CSVs directly (smoke testing, prelim validation)

Adds derived features, computes estimated cost from AWS pricing formulas,
filters unreliable high-error blocks, and performs a stratified 80/20 split.

Usage:
    # Prelim test with smoke results (raw CSVs, no supplementary data):
    python3 prepare_data.py \
        --load-dir ../scripts/smoke_results/ \
        --output-dir output/

    # Full pipeline (after aggregate_results.py has produced results.csv):
    python3 prepare_data.py \
        --results-csv results.csv \
        --cold-starts cold_starts.csv \
        --output-dir output/

Requires: Python >= 3.12, pandas >= 2.2, numpy >= 1.26, scikit-learn >= 1.5
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

FREQ_MAP = {
    "1k": 1_000,
    "10k": 10_000,
    "50k": 50_000,
    "100k": 100_000,
}

TIER_MAP = {
    "small": 500,
    "medium": 2_000,
    "large": 5_000,
}

IMAGE_SIZE_MB_MAP = {
    "slim": 50,
    "standard": 250,
}

STATE_MANAGEMENT_ARCHETYPE = "enterprise-microservice"

# AWS pricing (us-east-2, as of 2025)
LAMBDA_PRICE_PER_GB_SECOND = 0.0000166667
LAMBDA_PRICE_PER_REQUEST = 0.20 / 1_000_000
FARGATE_VCPU_PER_HOUR = 0.04048
FARGATE_GB_MEM_PER_HOUR = 0.004445

# Fargate task definition mappings (from deploy_fargate.sh)
FARGATE_TASK_SPECS = {
    512: {"vcpu": 0.5, "memory_gb": 1.0},
    2048: {"vcpu": 2.0, "memory_gb": 4.0},
}

ACTIVE_BLOCK_SECONDS = 90 * 60

ERROR_RATE_THRESHOLD_PCT = 10.0


def load_from_results_csv(path: str) -> pd.DataFrame:
    """Load the aggregated results.csv from aggregate_results.py."""
    df = pd.read_csv(path)
    print(f"Loaded {len(df)} rows from {path}")
    return df


def load_from_raw_csvs(load_dir: str) -> pd.DataFrame:
    """Load and concatenate raw per-deployment CSV files."""
    load_path = Path(load_dir)
    csv_files = sorted(load_path.glob("*.csv"))

    if not csv_files:
        print(f"ERROR: No CSV files found in {load_dir}", file=sys.stderr)
        sys.exit(1)

    frames = [pd.read_csv(f) for f in csv_files]
    combined = pd.concat(frames, ignore_index=True)
    print(f"Loaded {len(csv_files)} CSVs → {len(combined)} rows")

    if "image_size_mb" not in combined.columns and "image_size" in combined.columns:
        combined["image_size_mb"] = (
            combined["image_size"].map(IMAGE_SIZE_MB_MAP).fillna(0).astype(int)
        )
    if "state_management" not in combined.columns:
        combined["state_management"] = (
            combined["archetype"].eq(STATE_MANAGEMENT_ARCHETYPE).astype(int)
        )
    if "cold_start_rate_pct" not in combined.columns:
        combined["cold_start_rate_pct"] = 0.0
    if "cold_start_duration_ms" not in combined.columns:
        combined["cold_start_duration_ms"] = 0.0

    return combined


def merge_cold_starts_temporal(
    df: pd.DataFrame, cold_starts_path: str | None
) -> pd.DataFrame:
    """Merge cold start data aligned to block timestamps.

    If cold_starts.csv has a bin_start_utc column, joins temporally so each
    block gets its own cold start rate (preserving the CV → cold start signal).
    Falls back to per-deployment aggregate if no temporal column exists.
    """
    if not cold_starts_path:
        return df

    cs = pd.read_csv(cold_starts_path)
    print(f"Loaded cold starts: {len(cs)} rows")

    if "bin_start_utc" in cs.columns and "block_start_utc" in df.columns:
        cs["bin_start_utc"] = pd.to_datetime(cs["bin_start_utc"], utc=True)
        df["block_start_utc"] = pd.to_datetime(df["block_start_utc"], utc=True)

        cs = cs.rename(columns={"function_name": "deployment_name"})

        # merge_asof requires left sorted by the merge key globally
        df = df.sort_values("block_start_utc").reset_index(drop=True)
        cs = cs.sort_values("bin_start_utc").reset_index(drop=True)

        df = pd.merge_asof(
            df,
            cs[["deployment_name", "bin_start_utc", "cold_start_count",
                "avg_cold_start_ms", "total_invocations"]],
            left_on="block_start_utc",
            right_on="bin_start_utc",
            by="deployment_name",
            direction="nearest",
            tolerance=pd.Timedelta("95min"),
        )
        print("  Merged cold starts using temporal alignment (bin_start_utc)")
    else:
        print("  WARNING: No bin_start_utc in cold starts — using per-deployment aggregate.")
        print("  Cold start rate will be uniform across blocks (temporal signal lost).")
        cs_agg = (
            cs.groupby("function_name")
            .agg(
                cold_start_count=("cold_start_count", "sum"),
                avg_cold_start_ms=("avg_cold_start_ms", "mean"),
                total_invocations=("total_invocations", "sum"),
            )
            .reset_index()
            .rename(columns={"function_name": "deployment_name"})
        )
        df = df.merge(cs_agg, on="deployment_name", how="left")

    df["cold_start_count"] = df.get("cold_start_count", pd.Series(0)).fillna(0)
    df["avg_cold_start_ms"] = df.get("avg_cold_start_ms", pd.Series(0.0)).fillna(0.0)

    lambda_mask = df["platform"] == "lambda"
    total_req = df.loc[lambda_mask, "total_requests"].replace(0, 1)
    df.loc[lambda_mask, "cold_start_rate_pct"] = (
        df.loc[lambda_mask, "cold_start_count"] / total_req * 100
    ).round(4)
    df.loc[lambda_mask, "cold_start_duration_ms"] = df.loc[
        lambda_mask, "avg_cold_start_ms"
    ].round(2)

    fargate_mask = df["platform"] == "fargate"
    df.loc[fargate_mask, "cold_start_rate_pct"] = 0.0
    df.loc[fargate_mask, "cold_start_duration_ms"] = 0.0

    df = df.drop(
        columns=["cold_start_count", "avg_cold_start_ms", "total_invocations",
                 "bin_start_utc"],
        errors="ignore",
    )
    return df


def merge_alb_lcu_costs(df: pd.DataFrame, alb_lcu_path: str | None) -> pd.DataFrame:
    """Merge per-block ALB LCU costs for Fargate deployments.

    The ALB LCU cost is the variable portion of Fargate TCO — it depends on
    traffic rate, burstiness, and payload size. Without this data, Fargate cost
    is purely deterministic (just compute reservation).
    """
    if not alb_lcu_path:
        df["alb_lcu_cost_usd"] = 0.0
        return df

    lcu = pd.read_csv(alb_lcu_path)
    print(f"Loaded ALB LCU data: {len(lcu)} rows")

    lcu = lcu[["deployment_name", "block_index", "lcu_cost_usd"]].rename(
        columns={"lcu_cost_usd": "alb_lcu_cost_usd"}
    )

    if "block_index" in df.columns:
        df = df.merge(lcu, on=["deployment_name", "block_index"], how="left")
    else:
        df = df.merge(
            lcu.groupby("deployment_name")["alb_lcu_cost_usd"].mean().reset_index(),
            on="deployment_name", how="left",
        )

    df["alb_lcu_cost_usd"] = df["alb_lcu_cost_usd"].fillna(0.0)
    fargate_mask = df["platform"] == "fargate"
    non_fargate_mask = ~fargate_mask
    df.loc[non_fargate_mask, "alb_lcu_cost_usd"] = 0.0

    if fargate_mask.any():
        mean_lcu = df.loc[fargate_mask, "alb_lcu_cost_usd"].mean()
        max_lcu = df.loc[fargate_mask, "alb_lcu_cost_usd"].max()
        print(f"  Fargate ALB LCU cost: mean=${mean_lcu:.6f}, max=${max_lcu:.6f}")

    return df


def compute_cost_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Compute estimated cost per block from AWS pricing formulas.

    Lambda cost = (requests × avg_duration_s × memory_GB × price/GB-s) + (requests × price/request)
    Fargate cost = block_duration_hours × (vCPU × vCPU_price + memory_GB × memory_price)
    """
    df = df.copy()

    memory_gb = df["memory_mb"] / 1024
    mean_duration_s = df["mean_latency_ms"] / 1000

    # Lambda: pay per invocation duration
    lambda_mask = df["platform"] == "lambda"
    df.loc[lambda_mask, "estimated_cost_usd"] = (
        df.loc[lambda_mask, "total_requests"]
        * mean_duration_s[lambda_mask]
        * memory_gb[lambda_mask]
        * LAMBDA_PRICE_PER_GB_SECOND
        + df.loc[lambda_mask, "total_requests"] * LAMBDA_PRICE_PER_REQUEST
    ).round(6)

    # Fargate: compute reservation (fixed) + ALB LCU (variable, traffic-dependent)
    fargate_mask = df["platform"] == "fargate"
    block_hours = ACTIVE_BLOCK_SECONDS / 3600
    for mem_mb, specs in FARGATE_TASK_SPECS.items():
        mem_mask = fargate_mask & (df["memory_mb"] == mem_mb)
        compute_cost = block_hours * (
            specs["vcpu"] * FARGATE_VCPU_PER_HOUR
            + specs["memory_gb"] * FARGATE_GB_MEM_PER_HOUR
        )
        df.loc[mem_mask, "estimated_cost_usd"] = (
            compute_cost + df.loc[mem_mask, "alb_lcu_cost_usd"]
        ).round(6)

    df["estimated_cost_usd"] = df["estimated_cost_usd"].fillna(0.0)
    return df


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived feature columns for model training."""
    df = df.copy()

    if "image_size_mb" not in df.columns and "image_size" in df.columns:
        df["image_size_mb"] = (
            df["image_size"].map(IMAGE_SIZE_MB_MAP).fillna(0).astype(int)
        )
    if "state_management" not in df.columns:
        df["state_management"] = (
            df["archetype"].eq(STATE_MANAGEMENT_ARCHETYPE).astype(int)
        )

    df["invocation_frequency_numeric"] = df["invocation_frequency"].map(FREQ_MAP)
    df["duration_tier_numeric"] = df["duration_tier"].map(TIER_MAP)
    df["log_invocations"] = np.log(df["invocation_frequency_numeric"])
    df["sustained_load"] = (
        df["invocation_frequency_numeric"] * df["duration_tier_numeric"] / 1e6
    )

    # One-hot encode archetype (drop first to avoid collinearity)
    archetype_dummies = pd.get_dummies(df["archetype"], prefix="arch", dtype=int)
    if "arch_batch-transform" in archetype_dummies.columns:
        archetype_dummies = archetype_dummies.drop(columns=["arch_batch-transform"])
    df = pd.concat([df, archetype_dummies], axis=1)

    # Binary platform feature
    df["platform_is_lambda"] = (df["platform"] == "lambda").astype(int)

    return df


def filter_high_error_blocks(df: pd.DataFrame) -> pd.DataFrame:
    """Remove blocks with error rate above threshold — their latency data is unreliable."""
    before = len(df)
    df = df[df["error_rate_pct"] <= ERROR_RATE_THRESHOLD_PCT].copy()
    dropped = before - len(df)
    if dropped > 0:
        print(f"Filtered {dropped} blocks with error_rate > {ERROR_RATE_THRESHOLD_PCT}% "
              f"({before} → {len(df)} rows)")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare model training data from experiment results."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--results-csv", help="Path to results.csv from aggregate_results.py")
    group.add_argument("--load-dir", help="Directory with raw per-deployment CSVs (smoke test mode)")
    parser.add_argument("--cold-starts", default=None, help="Path to cold_starts.csv (optional)")
    parser.add_argument("--alb-lcu", default=None, help="Path to alb_lcu_per_block.csv from query_alb_lcu.py (optional)")
    parser.add_argument("--output-dir", required=True, help="Directory to write train.csv and test.csv")
    parser.add_argument(
        "--keep-high-error", action="store_true",
        help="Don't filter high-error blocks (default: filter >10%% error rate)",
    )
    args = parser.parse_args()

    # Load data
    if args.results_csv:
        df = load_from_results_csv(args.results_csv)
    else:
        df = load_from_raw_csvs(args.load_dir)

    # Merge cold starts (temporal alignment if available)
    df = merge_cold_starts_temporal(df, args.cold_starts)

    # Merge ALB LCU costs for Fargate blocks
    df = merge_alb_lcu_costs(df, args.alb_lcu)

    # Filter unreliable blocks
    if not args.keep_high_error:
        df = filter_high_error_blocks(df)

    # Compute cost from pricing formulas + ALB LCU
    df = compute_cost_columns(df)

    # Add derived features
    df = add_derived_features(df)

    # Grouped 80/20 split by deployment_name — ensures no block-sequence leakage.
    # Entire deployments go to train or test, so the model is evaluated on
    # configurations it has never seen (not just unseen blocks from seen deployments).
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, test_idx = next(gss.split(df, groups=df["deployment_name"]))
    train_df = df.iloc[train_idx]
    test_df = df.iloc[test_idx]

    # Write output
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_path = output_dir / "train.csv"
    test_path = output_dir / "test.csv"
    train_df.to_csv(train_path, index=False)
    test_df.to_csv(test_path, index=False)

    print(f"\nTrain: {len(train_df)} rows ({train_df['deployment_name'].nunique()} deployments) → {train_path}")
    print(f"Test:  {len(test_df)} rows ({test_df['deployment_name'].nunique()} deployments) → {test_path}")

    print("\n── Archetype distribution (train) ──")
    for arch, count in train_df["archetype"].value_counts().sort_index().items():
        print(f"  {arch}: {count} ({count / len(train_df) * 100:.1f}%)")

    print("\n── Archetype distribution (test) ──")
    for arch, count in test_df["archetype"].value_counts().sort_index().items():
        print(f"  {arch}: {count} ({count / len(test_df) * 100:.1f}%)")

    print("\n── Deployment split ──")
    train_deps = set(train_df["deployment_name"].unique())
    test_deps = set(test_df["deployment_name"].unique())
    overlap = train_deps & test_deps
    if overlap:
        print(f"  WARNING: {len(overlap)} deployments appear in both sets (leakage!)")
    else:
        print(f"  No deployment overlap between train and test (clean split)")

    print("\n── Cost column summary ──")
    for platform in ["lambda", "fargate"]:
        subset = train_df[train_df["platform"] == platform]["estimated_cost_usd"]
        print(f"  {platform}: mean=${subset.mean():.6f}, "
              f"min=${subset.min():.6f}, max=${subset.max():.6f}")

    print("\n── Feature columns ──")
    feature_cols = [
        "memory_mb", "image_size_mb", "state_management",
        "invocation_frequency_numeric", "traffic_cv", "duration_tier_numeric",
        "log_invocations", "sustained_load", "platform_is_lambda",
        "estimated_cost_usd",
    ]
    arch_cols = [c for c in train_df.columns if c.startswith("arch_")]
    feature_cols.extend(arch_cols)
    for col in feature_cols:
        if col in train_df.columns:
            print(f"  {col}: [{train_df[col].min():.4f}, {train_df[col].max():.4f}]")


if __name__ == "__main__":
    main()
