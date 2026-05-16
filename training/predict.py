#!/usr/bin/env python3
"""Predict platform recommendation for a given workload configuration.

Loads the serialized models produced by train_models.py and answers:
"Given my workload characteristics, should I use Lambda or Fargate,
and what can I expect for cost, latency, and cold starts?"

Usage:
    # Single prediction
    python3 predict.py --models-dir training-output/run-2/models \
        --archetype event-driven-api \
        --memory 512 \
        --image-size slim \
        --frequency 50k \
        --cv 2.0 \
        --duration medium

    # Compare across all frequencies for a fixed config
    python3 predict.py --models-dir training-output/run-2/models \
        --archetype ml-inference \
        --memory 2048 \
        --image-size standard \
        --sweep frequency

    # Find the break-even point for a workload
    python3 predict.py --models-dir training-output/run-2/models \
        --archetype batch-transform \
        --memory 512 \
        --image-size slim \
        --sweep all

Requires: Python >= 3.12, pandas, numpy, scikit-learn, joblib
"""

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

FREQ_MAP = {"1k": 1000, "10k": 10000, "50k": 50000, "100k": 100000}
TIER_MAP = {"small": 500, "medium": 2000, "large": 5000}
IMAGE_SIZE_MAP = {"slim": 50, "standard": 250}

ARCHETYPES = ["event-driven-api", "batch-transform", "ml-inference", "enterprise-microservice"]

# Which trained algorithm to use for each prediction target.  Chosen from
# the run-2 evaluation: Ridge wins on the linear cost targets; Random
# Forest wins on the non-linear latency / cold-start targets.
PREFERRED_ALGOS = {
    "lambda_cost":     "ridge",
    "fargate_cost":    "ridge",
    "cold_start_rate": "random_forest",
    "p95_latency":     "random_forest",
}


def build_feature_row(
    archetype: str, memory_mb: int, image_size: str,
    frequency: str, cv: float, duration_tier: str, platform: str,
) -> dict:
    """Build a single feature row for prediction."""
    freq_num = FREQ_MAP[frequency]
    tier_num = TIER_MAP[duration_tier]
    return {
        "memory_mb": memory_mb,
        "image_size_mb": IMAGE_SIZE_MAP[image_size],
        "invocation_frequency_numeric": freq_num,
        "traffic_cv": cv,
        "duration_tier_numeric": tier_num,
        "log_invocations": np.log(freq_num),
        "sustained_load": freq_num * tier_num / 1e6,
        "state_management": 1 if archetype == "enterprise-microservice" else 0,
        "platform_is_lambda": 1 if platform == "lambda" else 0,
        "arch_enterprise-microservice": 1 if archetype == "enterprise-microservice" else 0,
        "arch_event-driven-api": 1 if archetype == "event-driven-api" else 0,
        "arch_ml-inference": 1 if archetype == "ml-inference" else 0,
    }


def load_models(models_dir: Path) -> dict:
    """Load fitted models and their feature schemas from disk.

    Returns a dict keyed by target name (e.g. "lambda_cost") whose values
    are (estimator, features) tuples — the same shape the rest of this
    script expects.
    """
    schema_path = models_dir / "feature_schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"feature_schema.json not found in {models_dir}.  "
            f"Run train_models.py to produce serialized artifacts first."
        )
    with open(schema_path) as f:
        schema = json.load(f)

    models = {}
    for target, algo in PREFERRED_ALGOS.items():
        key = f"{target}_{algo}"
        if key not in schema:
            raise KeyError(
                f"Schema missing '{key}'.  Re-run train_models.py against "
                f"the same dataset version."
            )
        artifact_path = models_dir / schema[key]["artifact"]
        estimator = joblib.load(artifact_path)
        models[target] = (estimator, schema[key]["features"])
    return models


def predict_single(models: dict, row: dict, platform: str) -> dict:
    """Run all models for a single configuration."""
    df = pd.DataFrame([row])
    results = {}

    cost_key = f"{platform}_cost"
    model, features = models[cost_key]
    results["cost_usd"] = max(0, model.predict(df[features])[0])

    if platform == "lambda":
        model, features = models["cold_start_rate"]
        results["cold_start_pct"] = max(0, model.predict(df[features])[0])
    else:
        results["cold_start_pct"] = 0.0

    model, features = models["p95_latency"]
    results["p95_latency_ms"] = max(0, model.predict(df[features])[0])

    return results


def print_prediction(archetype, memory, image_size, frequency, cv, duration, models):
    """Print a side-by-side Lambda vs Fargate prediction."""
    row_lambda = build_feature_row(archetype, memory, image_size, frequency, cv, duration, "lambda")
    row_fargate = build_feature_row(archetype, memory, image_size, frequency, cv, duration, "fargate")

    pred_lambda = predict_single(models, row_lambda, "lambda")
    pred_fargate = predict_single(models, row_fargate, "fargate")

    cost_diff = pred_lambda["cost_usd"] - pred_fargate["cost_usd"]
    recommendation = "Lambda" if cost_diff < 0 else "Fargate"

    print(f"\n{'─' * 60}")
    print(f"  Workload: {archetype} | {memory}MB | {image_size} | {frequency}/day | CV={cv} | {duration}")
    print(f"{'─' * 60}")
    print(f"  {'Metric':<25} {'Lambda':>12} {'Fargate':>12} {'Winner':>10}")
    print(f"  {'─' * 55}")
    print(f"  {'Cost/block (USD)':<25} ${pred_lambda['cost_usd']:>10.5f} ${pred_fargate['cost_usd']:>10.5f} "
          f"{'← Lambda' if cost_diff < 0 else '← Fargate':>10}")
    print(f"  {'p95 latency (ms)':<25} {pred_lambda['p95_latency_ms']:>11.1f} {pred_fargate['p95_latency_ms']:>11.1f} "
          f"{'← Lambda' if pred_lambda['p95_latency_ms'] < pred_fargate['p95_latency_ms'] else '← Fargate':>10}")
    print(f"  {'Cold start rate (%)':<25} {pred_lambda['cold_start_pct']:>11.2f} {'N/A':>12}")
    print(f"\n  ➤ Recommendation: {recommendation} "
          f"(saves ${abs(cost_diff):.5f}/block, ${abs(cost_diff) * 36:.4f}/full-run)")

    return {"lambda": pred_lambda, "fargate": pred_fargate, "recommendation": recommendation}


def sweep_frequency(archetype, memory, image_size, cv, duration, models):
    """Show how recommendation changes across traffic volumes."""
    print(f"\n{'═' * 70}")
    print(f"  SWEEP: {archetype} | {memory}MB | {image_size} | CV={cv} | {duration}")
    print(f"  Varying: invocation frequency")
    print(f"{'═' * 70}")
    print(f"  {'Frequency':<12} {'Lambda $':>10} {'Fargate $':>10} {'Savings':>10} {'Winner':>10} {'Cold Start%':>12}")
    print(f"  {'─' * 65}")

    for freq in ["1k", "10k", "50k", "100k"]:
        row_l = build_feature_row(archetype, memory, image_size, freq, cv, duration, "lambda")
        row_f = build_feature_row(archetype, memory, image_size, freq, cv, duration, "fargate")
        pred_l = predict_single(models, row_l, "lambda")
        pred_f = predict_single(models, row_f, "fargate")
        diff = pred_l["cost_usd"] - pred_f["cost_usd"]
        winner = "Lambda" if diff < 0 else "Fargate"
        print(f"  {freq:<12} ${pred_l['cost_usd']:>9.5f} ${pred_f['cost_usd']:>9.5f} "
              f"${abs(diff):>9.5f} {winner:>10} {pred_l['cold_start_pct']:>11.2f}%")


def sweep_all(archetype, memory, image_size, models):
    """Full parameter sweep: frequency × CV × duration."""
    print(f"\n{'═' * 80}")
    print(f"  FULL SWEEP: {archetype} | {memory}MB | {image_size}")
    print(f"{'═' * 80}")
    print(f"  {'Freq':<6} {'CV':<5} {'Tier':<8} {'Lambda $':>10} {'Fargate $':>10} {'Winner':>8} {'p95(L)':>8} {'p95(F)':>8} {'CS%':>6}")
    print(f"  {'─' * 75}")

    for freq in ["1k", "10k", "50k", "100k"]:
        for cv in [0.5, 2.0, 4.0]:
            for dur in ["small", "medium", "large"]:
                row_l = build_feature_row(archetype, memory, image_size, freq, cv, dur, "lambda")
                row_f = build_feature_row(archetype, memory, image_size, freq, cv, dur, "fargate")
                pred_l = predict_single(models, row_l, "lambda")
                pred_f = predict_single(models, row_f, "fargate")
                diff = pred_l["cost_usd"] - pred_f["cost_usd"]
                winner = "Lam" if diff < 0 else "Far"
                print(f"  {freq:<6} {cv:<5} {dur:<8} ${pred_l['cost_usd']:>9.5f} ${pred_f['cost_usd']:>9.5f} "
                      f"{winner:>8} {pred_l['p95_latency_ms']:>7.0f} {pred_f['p95_latency_ms']:>7.0f} "
                      f"{pred_l['cold_start_pct']:>5.1f}")


def main():
    parser = argparse.ArgumentParser(description="Predict Lambda vs Fargate for a workload.")
    parser.add_argument("--models-dir", required=True,
                        help="Directory containing serialized models + feature_schema.json "
                             "(produced by train_models.py)")
    parser.add_argument("--archetype", required=True, choices=ARCHETYPES)
    parser.add_argument("--memory", type=int, required=True, choices=[512, 2048])
    parser.add_argument("--image-size", required=True, choices=["slim", "standard"])
    parser.add_argument("--frequency", choices=list(FREQ_MAP.keys()), default=None)
    parser.add_argument("--cv", type=float, default=None)
    parser.add_argument("--duration", choices=list(TIER_MAP.keys()), default=None)
    parser.add_argument("--sweep", choices=["frequency", "all"], default=None,
                        help="Sweep mode: vary frequency or all parameters")
    args = parser.parse_args()

    print(f"Loading models from {args.models_dir}...", end=" ")
    models = load_models(Path(args.models_dir))
    print("done.")

    if args.sweep == "all":
        sweep_all(args.archetype, args.memory, args.image_size, models)
    elif args.sweep == "frequency":
        cv = args.cv or 2.0
        dur = args.duration or "medium"
        sweep_frequency(args.archetype, args.memory, args.image_size, cv, dur, models)
    else:
        if not all([args.frequency, args.cv, args.duration]):
            print("ERROR: --frequency, --cv, and --duration required for single prediction", file=sys.stderr)
            sys.exit(1)
        print_prediction(args.archetype, args.memory, args.image_size,
                         args.frequency, args.cv, args.duration, models)


if __name__ == "__main__":
    main()
