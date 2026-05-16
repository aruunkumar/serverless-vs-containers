"""Unit tests for load_generator.py block execution logic (task 9.3).

Validates Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6:
- 90-minute active load with Gamma-distributed inter-arrivals
- 15-minute idle between blocks
- 30s request timeout
- Error counting on non-200/timeout
- Block summary CSV with all 18 columns
"""

import csv
import os
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from load_generator import (
    ACTIVE_BLOCK_SECONDS,
    CSV_COLUMNS,
    CV_LEVELS,
    DURATION_TIERS,
    FREQ_MAP,
    IDLE_SECONDS,
    PAYLOAD_TIERS,
    REQUEST_TIMEOUT,
    _build_summary,
    _write_block_csv,
    generate_inter_arrival_times,
    run_block,
    run_block_http,
    run_deployment,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_DEPLOYMENT = {
    "name": "svc-event-driven-api-512mb-slim-serverless",
    "url": "https://abc.execute-api.us-east-2.amazonaws.com/prod/invoke",
    "archetype": "event-driven-api",
    "platform": "lambda",
    "memory_mb": 512,
    "image_size": "slim",
}

SAMPLE_ENTERPRISE_DEPLOYMENT = {
    "name": "svc-enterprise-microservice-512mb-slim-container",
    "url": "http://alb.us-east-2.elb.amazonaws.com/svc-enterprise-microservice-512mb-slim-container/invoke",
    "archetype": "enterprise-microservice",
    "platform": "fargate",
    "memory_mb": 512,
    "image_size": "slim",
}


# ── Constants tests ──────────────────────────────────────────────────────────


class TestConstants:
    """Verify block execution constants match requirements."""

    def test_active_block_is_90_minutes(self):
        """Req 14.2: 90 minutes = 5400 seconds."""
        assert ACTIVE_BLOCK_SECONDS == 5400

    def test_idle_period_is_15_minutes(self):
        """Req 14.3: 15 minutes = 900 seconds."""
        assert IDLE_SECONDS == 900

    def test_request_timeout_is_30_seconds(self):
        """Req 14.5: 30-second timeout per request."""
        assert REQUEST_TIMEOUT == 30

    def test_freq_map_has_four_entries(self):
        """Req 14.1: 4 invocation frequencies."""
        assert set(FREQ_MAP.keys()) == {"1k", "10k", "50k", "100k"}

    def test_cv_levels_has_three_entries(self):
        """Req 14.1: 3 CV levels."""
        assert CV_LEVELS == [0.5, 2.0, 4.0]

    def test_duration_tiers_has_three_entries(self):
        """Req 14.1: 3 duration tiers."""
        assert DURATION_TIERS == ["small", "medium", "large"]

    def test_total_blocks_is_36(self):
        """Req 14.1: 4 × 3 × 3 = 36 blocks."""
        assert len(FREQ_MAP) * len(CV_LEVELS) * len(DURATION_TIERS) == 36

    def test_csv_columns_has_18_fields(self):
        """Req 15.3: 18 columns in block CSV."""
        assert len(CSV_COLUMNS) == 18

    def test_csv_columns_names(self):
        """Req 15.3: All required column names present."""
        expected = {
            "deployment_name", "archetype", "platform", "memory_mb",
            "image_size", "block_index", "invocation_frequency", "traffic_cv",
            "duration_tier", "block_start_utc", "total_requests", "error_count",
            "error_rate_pct", "p50_latency_ms", "p95_latency_ms",
            "p99_latency_ms", "mean_latency_ms", "throughput_rps",
        }
        assert set(CSV_COLUMNS) == expected


# ── Payload tier tests ───────────────────────────────────────────────────────


class TestPayloadTiers:
    """Req 14.4: Archetype-appropriate payload for each duration tier."""

    def test_all_archetypes_have_all_tiers(self):
        for archetype in PAYLOAD_TIERS:
            for tier in DURATION_TIERS:
                assert tier in PAYLOAD_TIERS[archetype], (
                    f"Missing tier {tier} for {archetype}"
                )

    def test_all_payloads_have_payload_tier_key(self):
        for archetype, tiers in PAYLOAD_TIERS.items():
            for tier, payload in tiers.items():
                assert "payload_tier" in payload
                assert payload["payload_tier"] == tier


# ── _build_summary tests ────────────────────────────────────────────────────


class TestBuildSummary:
    """Test the 18-column block summary builder."""

    def test_all_18_columns_present(self):
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [10.0, 20.0, 30.0], 1, 4,
        )
        assert set(summary.keys()) == set(CSV_COLUMNS)

    def test_deployment_fields_propagated(self):
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 5, "10k", 2.0, "medium",
            time.time(), [100.0], 0, 1,
        )
        assert summary["deployment_name"] == SAMPLE_DEPLOYMENT["name"]
        assert summary["archetype"] == "event-driven-api"
        assert summary["platform"] == "lambda"
        assert summary["memory_mb"] == 512
        assert summary["image_size"] == "slim"

    def test_block_params_propagated(self):
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 7, "50k", 4.0, "large",
            time.time(), [50.0], 0, 1,
        )
        assert summary["block_index"] == 7
        assert summary["invocation_frequency"] == "50k"
        assert summary["traffic_cv"] == 4.0
        assert summary["duration_tier"] == "large"

    def test_latency_percentiles_computed(self):
        latencies = list(range(1, 101))  # 1..100
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), latencies, 0, 100,
        )
        assert summary["p50_latency_ms"] == pytest.approx(50.5, abs=1)
        assert summary["p95_latency_ms"] == pytest.approx(95.05, abs=1)
        assert summary["p99_latency_ms"] == pytest.approx(99.01, abs=1)
        assert summary["mean_latency_ms"] == pytest.approx(50.5, abs=1)

    def test_error_rate_computed(self):
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [10.0] * 90, 10, 100,
        )
        assert summary["error_count"] == 10
        assert summary["error_rate_pct"] == pytest.approx(10.0, abs=0.01)

    def test_throughput_computed(self):
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [10.0] * 100, 0, 100,
        )
        assert summary["throughput_rps"] == pytest.approx(100 / 5400, abs=0.001)

    def test_empty_latencies_handled(self):
        """When no successful requests, latencies default to [0.0]."""
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [], 5, 5,
        )
        assert summary["p50_latency_ms"] == 0.0
        assert summary["mean_latency_ms"] == 0.0
        assert summary["error_rate_pct"] == 100.0

    def test_zero_total_sent_no_division_error(self):
        """Edge case: zero requests sent should not cause ZeroDivisionError."""
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [], 0, 0,
        )
        assert summary["error_rate_pct"] == 0.0
        assert summary["throughput_rps"] == 0.0

    def test_block_start_utc_is_iso_format(self):
        ts = 1700000000.0
        summary = _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            ts, [10.0], 0, 1,
        )
        expected = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        assert summary["block_start_utc"] == expected


# ── _write_block_csv tests ───────────────────────────────────────────────────


class TestWriteBlockCsv:
    """Test CSV writing with all 18 columns."""

    def _make_summary(self):
        return _build_summary(
            SAMPLE_DEPLOYMENT, 1, "1k", 0.5, "small",
            time.time(), [10.0, 20.0], 0, 2,
        )

    def test_creates_csv_with_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = self._make_summary()
            _write_block_csv(summary, tmpdir)
            csv_path = os.path.join(tmpdir, f"{summary['deployment_name']}.csv")
            assert os.path.exists(csv_path)
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                assert list(reader.fieldnames) == CSV_COLUMNS
                rows = list(reader)
                assert len(rows) == 1

    def test_appends_rows_without_duplicate_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s1 = self._make_summary()
            s2 = _build_summary(
                SAMPLE_DEPLOYMENT, 2, "10k", 2.0, "medium",
                time.time(), [30.0], 1, 2,
            )
            _write_block_csv(s1, tmpdir)
            _write_block_csv(s2, tmpdir)
            csv_path = os.path.join(tmpdir, f"{s1['deployment_name']}.csv")
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 2

    def test_all_18_values_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = self._make_summary()
            _write_block_csv(summary, tmpdir)
            csv_path = os.path.join(tmpdir, f"{summary['deployment_name']}.csv")
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                row = next(reader)
                for col in CSV_COLUMNS:
                    assert col in row, f"Missing column: {col}"
                    assert row[col] != "", f"Empty value for column: {col}"


# ── run_block_http tests (with mocked time + requests) ──────────────────────


class TestRunBlockHttp:
    """Test the HTTP block runner with mocked network and time.

    We use a short ACTIVE_BLOCK_SECONDS to allow the loop to execute a few
    requests quickly, then verify the results.
    """

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_sends_post_with_correct_payload_and_timeout(self, mock_requests):
        """Req 14.4, 14.5: correct payload and 30s timeout."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )

        # Verify POST was called with correct args
        assert mock_requests.post.call_count >= 1
        call_args = mock_requests.post.call_args
        assert call_args[0][0] == SAMPLE_DEPLOYMENT["url"]
        assert call_args[1]["json"] == PAYLOAD_TIERS["event-driven-api"]["small"]
        assert call_args[1]["timeout"] == 30

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_non_200_increments_error_counter(self, mock_requests):
        """Req 14.6: non-200 status increments error counter."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_requests.post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )

        assert summary["total_requests"] >= 1
        assert summary["error_count"] == summary["total_requests"]

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_timeout_exception_increments_error_counter(self, mock_requests):
        """Req 14.6: timeout increments error counter."""
        import requests as real_requests
        mock_requests.post.side_effect = real_requests.exceptions.Timeout("timed out")

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )

        assert summary["total_requests"] >= 1
        assert summary["error_count"] == summary["total_requests"]

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_connection_error_increments_error_counter(self, mock_requests):
        """Req 14.6: connection error increments error counter."""
        import requests as real_requests
        mock_requests.post.side_effect = real_requests.exceptions.ConnectionError("refused")

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )

        assert summary["total_requests"] >= 1
        assert summary["error_count"] == summary["total_requests"]

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_writes_csv_after_block(self, mock_requests):
        """Req 15.2: writes per-deployment CSV with one row per block."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )
            csv_path = os.path.join(tmpdir, f"{SAMPLE_DEPLOYMENT['name']}.csv")
            assert os.path.exists(csv_path)

    @patch("load_generator.ACTIVE_BLOCK_SECONDS", 0.3)
    @patch("load_generator.requests")
    def test_successful_requests_record_latency(self, mock_requests):
        """Successful requests should have latency recorded."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_requests.post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            summary = run_block_http(
                SAMPLE_DEPLOYMENT, "100k", 0.5, "small", 1, tmpdir,
            )

        assert summary["total_requests"] >= 1
        assert summary["mean_latency_ms"] >= 0.0
        assert summary["p50_latency_ms"] >= 0.0


# ── run_block dispatcher tests ───────────────────────────────────────────────


class TestRunBlockDispatcher:
    """Test that run_block dispatches to the correct runner."""

    @patch("load_generator.run_block_wrk2")
    def test_enterprise_microservice_uses_wrk2(self, mock_wrk2):
        mock_wrk2.return_value = {}
        run_block(SAMPLE_ENTERPRISE_DEPLOYMENT, "1k", 0.5, "small", 1, "/tmp")
        mock_wrk2.assert_called_once()

    @patch("load_generator.run_block_http")
    def test_non_enterprise_uses_http(self, mock_http):
        mock_http.return_value = {}
        run_block(SAMPLE_DEPLOYMENT, "1k", 0.5, "small", 1, "/tmp")
        mock_http.assert_called_once()


# ── run_deployment block iteration tests ─────────────────────────────────────


class TestRunDeploymentIteration:
    """Test that run_deployment iterates all 36 blocks correctly."""

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_runs_exactly_36_blocks(self, mock_run_block, mock_sleep):
        """Req 14.1: 4 freq × 3 CV × 3 tiers = 36 blocks."""
        mock_run_block.return_value = {}
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        assert mock_run_block.call_count == 36

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_block_indices_are_1_to_36(self, mock_run_block, mock_sleep):
        mock_run_block.return_value = {}
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        indices = [call.args[4] for call in mock_run_block.call_args_list]
        assert indices == list(range(1, 37))

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_all_parameter_combinations_covered(self, mock_run_block, mock_sleep):
        """Req 14.1: no duplicates, no missing combinations."""
        mock_run_block.return_value = {}
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        combos = set()
        for call in mock_run_block.call_args_list:
            freq, cv, tier = call.args[1], call.args[2], call.args[3]
            combos.add((freq, cv, tier))
        expected = {
            (f, c, t)
            for f in ["1k", "10k", "50k", "100k"]
            for c in CV_LEVELS
            for t in DURATION_TIERS
        }
        assert combos == expected

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_idle_sleep_called_between_blocks(self, mock_run_block, mock_sleep):
        """Req 14.3: 15-minute idle between blocks (35 sleeps for 36 blocks)."""
        mock_run_block.return_value = {}
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        idle_calls = [c for c in mock_sleep.call_args_list if c.args[0] == IDLE_SECONDS]
        assert len(idle_calls) == 35  # 36 blocks, 35 gaps

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_iteration_order_freq_cv_tier(self, mock_run_block, mock_sleep):
        """Req 14.1: nested order is frequency → CV → duration_tier."""
        mock_run_block.return_value = {}
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        params = [(c.args[1], c.args[2], c.args[3]) for c in mock_run_block.call_args_list]
        expected = []
        for f in ["1k", "10k", "50k", "100k"]:
            for c in CV_LEVELS:
                for t in DURATION_TIERS:
                    expected.append((f, c, t))
        assert params == expected

    @patch("load_generator.time.sleep")
    @patch("load_generator.run_block")
    def test_block_failure_does_not_stop_thread(self, mock_run_block, mock_sleep):
        """Error handling: a failing block should not kill the thread."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 5:
                raise RuntimeError("simulated failure")
            return {}

        mock_run_block.side_effect = side_effect
        # Should not raise — thread continues
        run_deployment(SAMPLE_DEPLOYMENT, "/tmp/out")
        assert mock_run_block.call_count == 36
