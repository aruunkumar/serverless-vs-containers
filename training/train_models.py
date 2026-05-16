#!/usr/bin/env python3
"""Train regression models for the serverless vs container experiment.

Trains five models using Ridge Regression (handles collinearity) and
Random Forest. Reports R², RMSE, MAE, and feature importance.
Saves plots as PNGs (headless, no display required).

Models:
  1. lambda_cost       — Predicted Lambda cost per block (USD)
  2. fargate_cost      — Predicted Fargate cost per block (USD)
  3. cold_start_rate   — Lambda cold start rate (%)
  4. p95_latency       — p95 tail latency (ms), per-archetype features
  5. cost_comparison   — Cost difference (Lambda - Fargate) for break-even analysis

Usage:
    python3 train_models.py --input-dir data/ --output-dir model_results/

Requires: Python >= 3.12, pandas >= 2.2, numpy >= 1.26, scikit-learn >= 1.5, matplotlib >= 3.9
"""

import argparse
import json
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.utils import resample

# Base features used by all models (no multicollinear derived pairs in Ridge)
BASE_FEATURES = [
    "memory_mb",
    "image_size_mb",
    "invocation_frequency_numeric",
    "traffic_cv",
    "duration_tier_numeric",
]

# Additional features that Random Forest can exploit (nonlinear, handles collinearity)
RF_EXTRA_FEATURES = [
    "log_invocations",
    "sustained_load",
]

MODELS = {
    "lambda_cost": {
        "target": "estimated_cost_usd",
        "filter": {"platform": "lambda"},
        "extra_features": ["state_management"],
        "description": "Lambda cost per block (USD)",
    },
    "fargate_cost": {
        "target": "estimated_cost_usd",
        "filter": {"platform": "fargate"},
        "extra_features": ["state_management"],
        "description": "Fargate cost per block (USD)",
    },
    "cold_start_rate": {
        "target": "cold_start_rate_pct",
        "filter": {"platform": "lambda"},
        "extra_features": ["state_management"],
        "description": "Lambda cold start rate (%)",
    },
    "p95_latency": {
        "target": "p95_latency_ms",
        "filter": None,
        "extra_features": ["state_management", "platform_is_lambda"],
        "use_archetype_features": True,
        "description": "p95 latency across both platforms (ms)",
    },
}


def get_archetype_columns(df: pd.DataFrame) -> list[str]:
    """Find one-hot encoded archetype columns in the dataframe."""
    return [c for c in df.columns if c.startswith("arch_")]


def get_features_for_algo(
    model_name: str, algo_type: str, df: pd.DataFrame
) -> list[str]:
    """Build feature list appropriate for the algorithm type.

    Ridge: uses base features only (avoids collinear derived features).
    Random Forest: uses base + derived features (handles collinearity natively).
    """
    config = MODELS[model_name]

    if algo_type == "ridge":
        features = BASE_FEATURES.copy()
    else:
        features = BASE_FEATURES.copy() + RF_EXTRA_FEATURES.copy()

    features.extend(config.get("extra_features", []))

    if config.get("use_archetype_features"):
        features.extend(get_archetype_columns(df))

    return [f for f in features if f in df.columns]


N_BOOTSTRAP = 1000
CONFIDENCE_LEVEL = 0.95


def bootstrap_metrics(
    y_true: np.ndarray, y_pred: np.ndarray, n_iterations: int = N_BOOTSTRAP
) -> dict:
    """Compute bootstrap confidence intervals for R², RMSE, and MAE.

    Returns point estimates and 95% CI bounds for each metric.
    """
    n = len(y_true)
    if n < 10:
        return {}

    r2_scores = []
    rmse_scores = []
    mae_scores = []

    rng = np.random.default_rng(42)
    for _ in range(n_iterations):
        idx = rng.integers(0, n, size=n)
        y_t = y_true[idx]
        y_p = y_pred[idx]
        if y_t.std() == 0:
            continue
        r2_scores.append(r2_score(y_t, y_p))
        rmse_scores.append(np.sqrt(mean_squared_error(y_t, y_p)))
        mae_scores.append(mean_absolute_error(y_t, y_p))

    alpha = (1 - CONFIDENCE_LEVEL) / 2
    lo = alpha * 100
    hi = (1 - alpha) * 100

    return {
        "r2_ci": [round(np.percentile(r2_scores, lo), 4),
                  round(np.percentile(r2_scores, hi), 4)],
        "rmse_ci": [round(np.percentile(rmse_scores, lo), 4),
                    round(np.percentile(rmse_scores, hi), 4)],
        "mae_ci": [round(np.percentile(mae_scores, lo), 4),
                   round(np.percentile(mae_scores, hi), 4)],
        "n_bootstrap": len(r2_scores),
    }


def filter_data(df: pd.DataFrame, filter_spec: dict | None) -> pd.DataFrame:
    """Apply platform/archetype filters to the dataset."""
    if filter_spec is None:
        return df
    mask = pd.Series(True, index=df.index)
    for col, val in filter_spec.items():
        mask &= df[col] == val
    return df[mask].copy()


def train_and_evaluate(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    model_name: str,
    output_dir: Path,
    models_dir: Path | None = None,
    schema: dict | None = None,
) -> dict:
    """Train Ridge and Random Forest for one target, return metrics."""
    config = MODELS[model_name]
    target = config["target"]

    train_filtered = filter_data(train_df, config["filter"])
    test_filtered = filter_data(test_df, config["filter"])

    if len(train_filtered) < 5 or len(test_filtered) < 2:
        print(f"  SKIP {model_name}: insufficient data "
              f"(train={len(train_filtered)}, test={len(test_filtered)})")
        return {}

    if target not in train_filtered.columns:
        print(f"  SKIP {model_name}: target column '{target}' not in data")
        return {}

    y_train = train_filtered[target]
    y_test = test_filtered[target]

    if y_train.std() == 0:
        print(f"  SKIP {model_name}: target has zero variance")
        return {}

    results = {
        "model_name": model_name,
        "description": config["description"],
        "target": target,
        "train_size": len(train_filtered),
        "test_size": len(test_filtered),
        "algorithms": {},
    }

    best_model = None
    best_r2 = -np.inf
    best_preds = None

    algos = [
        ("ridge", Ridge(alpha=1.0)),
        ("random_forest", RandomForestRegressor(
            n_estimators=200, random_state=42, n_jobs=-1
        )),
    ]

    for algo_name, algo in algos:
        features = get_features_for_algo(model_name, algo_name, train_filtered)
        results.setdefault("features", {})[algo_name] = features

        X_train = train_filtered[features]
        X_test = test_filtered[features]

        algo.fit(X_train, y_train)
        preds = algo.predict(X_test)

        r2 = r2_score(y_test, preds)
        rmse = np.sqrt(mean_squared_error(y_test, preds))
        mae = mean_absolute_error(y_test, preds)

        algo_results = {
            "r2": round(r2, 4),
            "rmse": round(rmse, 4),
            "mae": round(mae, 4),
        }

        # Bootstrap confidence intervals
        ci = bootstrap_metrics(y_test.values, preds)
        if ci:
            algo_results["confidence_intervals"] = ci

        if algo_name == "random_forest":
            importances = dict(zip(features, algo.feature_importances_))
            algo_results["feature_importance"] = {
                k: round(v, 4)
                for k, v in sorted(importances.items(), key=lambda x: -x[1])
            }
        elif algo_name == "ridge":
            coefficients = dict(zip(features, algo.coef_))
            algo_results["coefficients"] = {
                k: round(v, 6)
                for k, v in sorted(coefficients.items(), key=lambda x: -abs(x[1]))
            }
            algo_results["intercept"] = round(algo.intercept_, 6)

        results["algorithms"][algo_name] = algo_results
        ci_str = ""
        if ci and "r2_ci" in ci:
            ci_str = f"  95% CI R²=[{ci['r2_ci'][0]:.4f}, {ci['r2_ci'][1]:.4f}]"
        print(f"  {algo_name:20s} → R²={r2:.4f}, RMSE={rmse:.4f}, MAE={mae:.4f}{ci_str}")

        # Serialize fitted estimator + record its feature schema so
        # predict.py can load the artifact without re-training.
        if models_dir is not None:
            artifact_path = models_dir / f"{model_name}_{algo_name}.joblib"
            joblib.dump(algo, artifact_path)
            if schema is not None:
                schema[f"{model_name}_{algo_name}"] = {
                    "target": target,
                    "features": features,
                    "filter": config.get("filter"),
                    "artifact": artifact_path.name,
                }

        if r2 > best_r2:
            best_r2 = r2
            best_model = algo_name
            best_preds = preds

    results["best_algorithm"] = best_model

    # Predicted vs actual scatter plot
    if best_preds is not None:
        fig, ax = plt.subplots(1, 1, figsize=(6, 6))
        ax.scatter(y_test, best_preds, alpha=0.6, s=20)
        min_val = min(y_test.min(), best_preds.min())
        max_val = max(y_test.max(), best_preds.max())
        ax.plot([min_val, max_val], [min_val, max_val], "r--", lw=1.5, label="Perfect fit")
        ax.set_xlabel(f"Actual ({target})")
        ax.set_ylabel(f"Predicted ({target})")
        ax.set_title(f"{model_name} ({best_model})\nR²={best_r2:.4f}")
        ax.legend()
        plt.tight_layout()
        plot_path = output_dir / f"{model_name}_predicted_vs_actual.png"
        fig.savefig(plot_path, dpi=150)
        plt.close(fig)

    # Feature importance bar chart (Random Forest)
    rf_results = results["algorithms"].get("random_forest", {})
    if "feature_importance" in rf_results:
        fi = rf_results["feature_importance"]
        fig, ax = plt.subplots(1, 1, figsize=(8, max(3, len(fi) * 0.4)))
        sorted_features = sorted(fi.items(), key=lambda x: x[1], reverse=True)
        names = [x[0] for x in sorted_features]
        values = [x[1] for x in sorted_features]
        ax.barh(names, values)
        ax.set_xlabel("Importance")
        ax.set_title(f"{model_name} — Feature Importance (Random Forest)")
        plt.tight_layout()
        fi_path = output_dir / f"{model_name}_feature_importance.png"
        fig.savefig(fi_path, dpi=150)
        plt.close(fig)

    return results


def _compute_break_even_for_split(
    df: pd.DataFrame, split_name: str
) -> dict:
    """Compute break-even stats for a single data split (train or test)."""
    lambda_df = df[df["platform"] == "lambda"].copy()
    fargate_df = df[df["platform"] == "fargate"].copy()

    if lambda_df.empty or fargate_df.empty:
        print(f"  SKIP {split_name}: need both platform types")
        return {}

    join_keys = [
        "archetype", "memory_mb", "image_size_mb",
        "invocation_frequency_numeric", "traffic_cv", "duration_tier_numeric",
    ]
    available_keys = [k for k in join_keys if k in lambda_df.columns and k in fargate_df.columns]

    lambda_costs = lambda_df.groupby(available_keys)["estimated_cost_usd"].mean().reset_index()
    lambda_costs = lambda_costs.rename(columns={"estimated_cost_usd": "lambda_cost"})

    fargate_costs = fargate_df.groupby(available_keys)["estimated_cost_usd"].mean().reset_index()
    fargate_costs = fargate_costs.rename(columns={"estimated_cost_usd": "fargate_cost"})

    merged = lambda_costs.merge(fargate_costs, on=available_keys, how="inner")
    if merged.empty:
        print(f"  SKIP {split_name}: no matching configurations across platforms")
        return {}

    merged["cost_diff"] = merged["lambda_cost"] - merged["fargate_cost"]
    merged["lambda_cheaper"] = (merged["cost_diff"] < 0).astype(int)

    lambda_wins = int(merged["lambda_cheaper"].sum())
    fargate_wins = len(merged) - lambda_wins

    print(f"  [{split_name}] {len(merged)} matched configs — "
          f"Lambda cheaper: {lambda_wins} ({lambda_wins / len(merged) * 100:.1f}%), "
          f"Fargate cheaper: {fargate_wins} ({fargate_wins / len(merged) * 100:.1f}%)")

    return {
        "matched_configs": len(merged),
        "lambda_cheaper_count": lambda_wins,
        "fargate_cheaper_count": fargate_wins,
        "mean_cost_diff_usd": round(merged["cost_diff"].mean(), 6),
        "data": merged,
    }


def generate_break_even_analysis(
    train_df: pd.DataFrame, test_df: pd.DataFrame, output_dir: Path
) -> dict:
    """Run break-even analysis on both train and test sets for validation.

    Positive cost_diff = Lambda is more expensive → prefer Fargate.
    Negative cost_diff = Fargate is more expensive → prefer Lambda.

    Running on both sets demonstrates that the break-even findings are
    robust and not an artifact of the train split.
    """
    train_results = _compute_break_even_for_split(train_df, "train")
    test_results = _compute_break_even_for_split(test_df, "test")

    if not train_results:
        return {}

    # Check consistency between train and test
    if train_results and test_results:
        train_pct = train_results["lambda_cheaper_count"] / train_results["matched_configs"] * 100
        test_pct = test_results["lambda_cheaper_count"] / test_results["matched_configs"] * 100
        diff = abs(train_pct - test_pct)
        consistency = "CONSISTENT" if diff < 15 else "DIVERGENT"
        print(f"  Train/test consistency: {consistency} "
              f"(Lambda-cheaper: train={train_pct:.1f}% vs test={test_pct:.1f}%, delta={diff:.1f}pp)")

    # Save break-even data (train set — larger sample)
    merged = train_results["data"]
    be_path = output_dir / "break_even_data.csv"
    merged.to_csv(be_path, index=False)

    # Plot cost comparison
    if len(merged) > 1:
        fig, ax = plt.subplots(1, 1, figsize=(8, 5))
        x = range(len(merged))
        merged_sorted = merged.sort_values("cost_diff")
        colors = ["#2196F3" if d < 0 else "#FF5722" for d in merged_sorted["cost_diff"]]
        ax.bar(x, merged_sorted["cost_diff"], color=colors, alpha=0.7)
        ax.axhline(0, color="black", lw=0.8)
        ax.set_xlabel("Configuration (sorted)")
        ax.set_ylabel("Cost Difference (Lambda - Fargate, USD)")
        ax.set_title("Break-Even: Blue = Lambda Cheaper, Red = Fargate Cheaper")
        plt.tight_layout()
        fig.savefig(output_dir / "break_even_comparison.png", dpi=150)
        plt.close(fig)

    output = {
        "train": {k: v for k, v in train_results.items() if k != "data"},
    }
    if test_results:
        output["test"] = {k: v for k, v in test_results.items() if k != "data"}
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Train regression models.")
    parser.add_argument("--input-dir", required=True, help="Directory with train.csv and test.csv")
    parser.add_argument("--output-dir", required=True, help="Directory for results and plots")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir = output_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    train_df = pd.read_csv(input_dir / "train.csv")
    test_df = pd.read_csv(input_dir / "test.csv")
    print(f"Loaded train ({len(train_df)} rows) and test ({len(test_df)} rows)")

    all_results = {}
    schema = {}

    for model_name in MODELS:
        print(f"\n{'═' * 60}")
        print(f"Training: {model_name} — {MODELS[model_name]['description']}")
        print(f"{'─' * 60}")
        result = train_and_evaluate(
            train_df, test_df, model_name, output_dir,
            models_dir=models_dir, schema=schema,
        )
        if result:
            all_results[model_name] = result

    # Break-even analysis (run on both train and test for validation)
    print(f"\n{'═' * 60}")
    print("Break-Even Analysis (Lambda vs Fargate cost)")
    print(f"{'─' * 60}")
    be_results = generate_break_even_analysis(train_df, test_df, output_dir)
    if be_results:
        all_results["break_even"] = be_results

    # Save summary JSON
    summary_path = output_dir / "model_summary.json"
    with open(summary_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n{'═' * 60}")
    print(f"Summary saved to {summary_path}")

    # Save the feature schema alongside the joblib artifacts.  predict.py
    # uses this to load each estimator with the exact feature ordering it
    # was trained on.
    schema_path = models_dir / "feature_schema.json"
    with open(schema_path, "w") as f:
        json.dump(schema, f, indent=2)
    print(f"Models + schema saved to {models_dir}/")

    # Print final comparison table
    print(f"\n{'═' * 60}")
    print(f"{'Model':<20} {'Best Algo':<16} {'R²':>8} {'RMSE':>10} {'MAE':>10}")
    print(f"{'─' * 66}")
    for name, res in all_results.items():
        if "best_algorithm" not in res:
            continue
        best = res["best_algorithm"]
        metrics = res["algorithms"][best]
        print(f"{name:<20} {best:<16} {metrics['r2']:>8.4f} "
              f"{metrics['rmse']:>10.4f} {metrics['mae']:>10.4f}")


if __name__ == "__main__":
    main()
