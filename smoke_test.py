#!/usr/bin/env python3
"""smoke_test.py — Short validation run of the experiment platform.

Runs a reduced experiment to verify all 32 endpoints work end-to-end:
- 2 blocks per deployment (instead of 36)
- 5-minute active load per block (instead of 90 minutes)
- 1-minute idle between blocks (instead of 15 minutes)
- Only tests 1 frequency (10k) × 1 CV (0.5) × 2 tiers (small, medium)

Total runtime: ~12 minutes (2 blocks × 6 min each, all 32 in parallel).

Usage:
    python3 smoke_test.py --config deployments.json --output smoke_results/

Run this BEFORE the full 7-day experiment to catch deployment issues early.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time

# Patch load_generator constants BEFORE importing it
import load_generator

# Override timing constants for smoke test
load_generator.ACTIVE_BLOCK_SECONDS = 5 * 60   # 5 minutes active (not 90)
load_generator.IDLE_SECONDS = 60                # 1 minute idle (not 15)

from load_generator import (
    CSV_COLUMNS,
    FREQ_MAP,
    PAYLOAD_TIERS,
    _build_summary,
    _write_block_csv,
    generate_inter_arrival_times,
    run_block,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("smoke_test.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# Smoke test parameters: just 2 blocks per deployment
SMOKE_BLOCKS = [
    ("10k", 0.5, "small"),
    ("10k", 0.5, "medium"),
]


def run_smoke_deployment(deployment: dict, output_dir: str) -> dict:
    """Run smoke test blocks for a single deployment.

    Returns a summary dict with pass/fail status.
    """
    name = deployment["name"]
    log.info("[%s] Smoke test starting (%d blocks)", name, len(SMOKE_BLOCKS))

    results = {"name": name, "blocks_run": 0, "blocks_ok": 0, "errors": []}

    for idx, (freq, cv, tier) in enumerate(SMOKE_BLOCKS, start=1):
        try:
            summary = run_block(deployment, freq, cv, tier, idx, output_dir)
            results["blocks_run"] += 1

            total = summary.get("total_requests", 0)
            errs = summary.get("error_count", 0)
            p95 = summary.get("p95_latency_ms", 0)

            if total == 0:
                results["errors"].append(f"Block {idx}: zero requests sent")
            elif errs == total:
                results["errors"].append(
                    f"Block {idx}: all {total} requests failed"
                )
            else:
                results["blocks_ok"] += 1
                log.info(
                    "[%s] Block %d OK — %d reqs, %d errors, p95=%.1fms",
                    name, idx, total, errs, p95,
                )

            # Short idle between blocks (already patched to 1 min)
            if idx < len(SMOKE_BLOCKS):
                log.info("[%s] Idle 1 minute before next block", name)
                time.sleep(load_generator.IDLE_SECONDS)

        except Exception as exc:
            results["blocks_run"] += 1
            results["errors"].append(f"Block {idx}: {exc}")
            log.error("[%s] Block %d FAILED: %s", name, idx, exc)

    status = "PASS" if results["blocks_ok"] == len(SMOKE_BLOCKS) else "FAIL"
    log.info("[%s] Smoke test %s (%d/%d blocks OK)",
             name, status, results["blocks_ok"], len(SMOKE_BLOCKS))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test — short validation run of all 32 endpoints",
    )
    parser.add_argument(
        "--config", required=True, help="Path to deployments.json",
    )
    parser.add_argument(
        "--output", default="smoke_results/",
        help="Output directory for smoke test CSVs",
    )
    args = parser.parse_args()

    try:
        with open(args.config) as fh:
            deployments = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load %s: %s", args.config, exc)
        sys.exit(1)

    log.info("=" * 60)
    log.info("  SMOKE TEST — %d deployments, %d blocks each",
             len(deployments), len(SMOKE_BLOCKS))
    log.info("  Active load: 5 min/block, Idle: 1 min between blocks")
    log.info("  Estimated runtime: ~12 minutes")
    log.info("=" * 60)

    os.makedirs(args.output, exist_ok=True)

    # Launch all deployments in parallel (same as full run)
    threads: list[threading.Thread] = []
    all_results: list[dict] = []
    lock = threading.Lock()

    def worker(dep):
        result = run_smoke_deployment(dep, args.output)
        with lock:
            all_results.append(result)

    for dep in deployments:
        t = threading.Thread(target=worker, args=(dep,), name=dep["name"])
        threads.append(t)
        t.start()
        time.sleep(0.5)  # stagger

    for t in threads:
        t.join()

    # Print summary
    passed = [r for r in all_results if r["blocks_ok"] == len(SMOKE_BLOCKS)]
    failed = [r for r in all_results if r["blocks_ok"] < len(SMOKE_BLOCKS)]

    print("\n" + "=" * 60)
    print("  SMOKE TEST RESULTS")
    print("=" * 60)
    print(f"  Deployments tested: {len(all_results)}")
    print(f"  PASSED: {len(passed)}")
    print(f"  FAILED: {len(failed)}")

    if failed:
        print("\n  Failed deployments:")
        for r in sorted(failed, key=lambda x: x["name"]):
            print(f"    ✗ {r['name']}")
            for err in r["errors"]:
                print(f"        {err}")

    if passed:
        print(f"\n  All {len(passed)} passing deployments responded correctly.")

    print(f"\n  CSV results in: {args.output}")
    print(f"  Log file: smoke_test.log")
    print("=" * 60)

    if failed:
        print("\n  ⚠ Fix failing deployments before starting the full experiment.")
        sys.exit(1)
    else:
        print("\n  ✓ All clear — safe to start the full 7-day experiment.")


if __name__ == "__main__":
    main()
