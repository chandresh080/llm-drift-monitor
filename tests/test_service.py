"""Tests for the service layer: circuit breaker, matched windows, test dispatch."""

import json
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import numpy as np
import pytest

from drift_tests import proportion_test
from monitor import CircuitBreaker, select_matched_windows
from storage import DriftMonitorStorage


def make_runs(n, version="2.1.0", prompt_ids=None, start_id=1, lengths=None):
    runs = []
    for i in range(n):
        runs.append({
            'id': start_id + i,
            'prompt_version': version,
            'prompt_id': (prompt_ids or ["p001", "p002", "p003"])[i % 3],
            'response_length': (lengths or [100] * n)[i],
        })
    return runs


class TestCircuitBreaker:
    def test_opens_after_consecutive_failures(self):
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        breaker.before_call()
        breaker.record_failure()
        breaker.before_call()
        breaker.record_failure()
        # Two failures: still closed.
        breaker.before_call()
        breaker.record_failure()
        # Three consecutive failures: now open, calls fail fast.
        with pytest.raises(RuntimeError, match="circuit open"):
            breaker.before_call()

    def test_success_resets_the_count(self):
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        for _ in range(2):
            breaker.before_call()
            breaker.record_failure()
        breaker.before_call()
        breaker.record_success()
        # Counter was reset: two more failures must not open the circuit.
        for _ in range(2):
            breaker.before_call()
            breaker.record_failure()
        breaker.before_call()

    def test_circuit_closes_after_cooldown(self):
        import time
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
        breaker.before_call()
        breaker.record_failure()
        with pytest.raises(RuntimeError):
            breaker.before_call()
        time.sleep(0.06)   # cooldown elapses
        breaker.before_call()   # must not raise


class TestCompleteCycles:
    def test_partial_cycles_are_excluded(self):
        from monitor import complete_cycles_only
        ids = ["p001", "p002", "p003"]
        complete = [{'id': i, 'cycle_id': 'c1', 'prompt_id': ids[i % 3]} for i in range(3)]
        partial = [{'id': 10, 'cycle_id': 'c2', 'prompt_id': 'p001'}]
        runs = complete + partial
        kept = complete_cycles_only(runs, ids)
        assert {r['cycle_id'] for r in kept} == {'c1'}

    def test_legacy_rows_without_cycle_id_are_excluded(self):
        from monitor import complete_cycles_only
        runs = [{'id': 1, 'cycle_id': None, 'prompt_id': 'p001'}]
        assert complete_cycles_only(runs, ['p001']) == []

    def test_duplicated_prompt_within_cycle_is_incomplete(self):
        from monitor import complete_cycles_only
        runs = [
            {'id': 1, 'cycle_id': 'c1', 'prompt_id': 'p001'},
            {'id': 2, 'cycle_id': 'c1', 'prompt_id': 'p001'},
            {'id': 3, 'cycle_id': 'c1', 'prompt_id': 'p002'},
        ]
        assert complete_cycles_only(runs, ['p001', 'p002', 'p003']) == []


class TestProxyMetrics:
    def test_proxy_metrics_directions(self):
        from experiments.reliability import proxy_metrics
        rows = [
            {'degraded': 1, 'ks': 1, 'mmd': 0},
            {'degraded': 1, 'ks': 1, 'mmd': 0},
            {'degraded': 0, 'ks': 1, 'mmd': 0},
            {'degraded': 0, 'ks': 0, 'mmd': 0},
        ]
        m = proxy_metrics(rows, 'ks')
        # Laplace smoothing: (2 flagged regressions + 1) / (3 flags + 2)
        # for precision, and (2 + 1) / (2 regressions + 2) for recall.
        assert m['p_regression_given_flag'] == pytest.approx(0.6)
        assert m['p_flag_given_regression'] == pytest.approx(0.75)
        assert m['benign_flag_rate'] == pytest.approx(0.5, abs=0.01)

    def test_proxy_metrics_never_divides_by_zero(self):
        from experiments.reliability import proxy_metrics
        rows = [{'degraded': 0, 'ks': 0, 'mmd': 0}]
        for signal in ('ks', 'mmd'):
            m = proxy_metrics(rows, signal)
            assert 0.0 <= m['p_regression_given_flag'] <= 1.0
            assert 0.0 <= m['p_flag_given_regression'] <= 1.0

    def test_draw_prior_bounds(self):
        from experiments.reliability import (CONDITIONS,
                                             draw_condition_with_prior)
        rng = np.random.default_rng(0)
        for _ in range(200):
            assert draw_condition_with_prior(rng, 0.3) in CONDITIONS
        rng0 = np.random.default_rng(1)
        draws = {draw_condition_with_prior(rng0, 0.0) for _ in range(200)}
        assert 'drift_only' not in draws


class TestProportionTest:
    def test_dispatches_to_fisher_for_small_counts(self):
        stat, pvalue, method = proportion_test([1, 0, 1, 1], [0, 0, 0, 1, 0])
        assert method == 'fisher'
        assert 0.0 <= pvalue <= 1.0

    def test_dispatches_to_z_for_large_counts(self):
        ref = [1] * 45 + [0] * 5
        cur = [1] * 20 + [0] * 30
        stat, pvalue, method = proportion_test(ref, cur)
        assert method == 'z'
        assert stat < 0   # validity dropped
        assert pvalue < 0.01

    def test_all_valid_is_not_significant(self):
        stat, pvalue, method = proportion_test([1] * 50, [1] * 10)
        assert pvalue >= 0.05


class TestMatchedWindows:
    def test_windows_use_only_matching_prompt_ids(self):
        runs = make_runs(60)
        runs += [dict(r, prompt_id='other_prompt', id=1000 + i) for i, r in enumerate(runs[:20])]
        ref, cur = select_matched_windows(runs, "2.1.0", ["p001", "p002", "p003"], 50, 10)
        assert len(ref) == 50 and len(cur) == 10
        assert all(r['prompt_id'] in {"p001", "p002", "p003"} for r in ref + cur)

    def test_windows_exclude_other_versions(self):
        runs = make_runs(60) + [dict(r, prompt_version='1.0.0') for r in make_runs(20, start_id=500)]
        ref, cur = select_matched_windows(runs, "2.1.0", ["p001", "p002", "p003"], 50, 10)
        assert all(r['prompt_version'] == '2.1.0' for r in ref + cur)

    def test_windows_are_disjoint(self):
        runs = make_runs(70)
        ref, cur = select_matched_windows(runs, "2.1.0", ["p001", "p002", "p003"], 50, 10)
        assert not ({r['id'] for r in ref} & {r['id'] for r in cur})

    def test_insufficient_current_returns_empty(self):
        ref, cur = select_matched_windows(make_runs(55), "2.1.0", ["p001", "p002", "p003"], 50, 10)
        assert len(ref) == 50 and cur == []


class TestStorageMigration:
    def test_new_columns_added_to_existing_database(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        # Simulate a pre-migration schema without the matched prompt columns.
        conn.execute("""
            CREATE TABLE probe_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                model_id TEXT NOT NULL,
                response_text TEXT NOT NULL,
                response_length INTEGER,
                is_valid INTEGER,
                run_status TEXT NOT NULL,
                adapter_type TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        storage = DriftMonitorStorage(str(db_path))
        run_id = storage.insert_probe_run(
            timestamp="2026-01-01T00:00:00", prompt_version="2.1.0",
            prompt_text="p", model_id="m", response_text="r",
            response_length=1, is_valid=True, run_status="success",
            adapter_type="fake", prompt_id="fact_001",
            category="factual", cycle_id="abc123",
        )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT prompt_id, category, cycle_id FROM probe_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row == ("fact_001", "factual", "abc123")

    def test_reopening_preserves_rows_and_relinks_legacy_results(self, tmp_path):
        db_path = tmp_path / "history.db"
        storage = DriftMonitorStorage(str(db_path))
        storage.insert_probe_run(
            timestamp="2026-01-01T00:00:00", prompt_version="3.0.0",
            prompt_text="p", model_id="m", response_text="r",
            response_length=1, is_valid=True, run_status="success",
            adapter_type="fake", prompt_id="p1", category="general",
            cycle_id="legacy-cycle")
        result = TestResultFieldMapping.representative_results()
        result.update({"cycle_id": "legacy-cycle",
                       "timestamp": "2026-01-01T00:01:00"})
        storage.insert_test_results(result)
        storage.insert_alert({
            "timestamp": "2026-01-01T00:02:00", "cycle_id": "legacy-cycle",
            "alert_type": "drift", "severity": "warning", "message": "legacy alert"})

        # Simulate records written before the cycle link existed.
        with storage._db() as conn:
            conn.execute("UPDATE test_results SET cycle_id = NULL")
            conn.execute("UPDATE alerts SET cycle_id = NULL")

        for _ in range(2):
            reopened = DriftMonitorStorage(str(db_path))
            assert reopened.count_all_runs() == 1
            assert reopened.count_test_results() == 1
            assert reopened.count_alerts() == 1
            summary = reopened.get_cycle_summaries(5)[0]
            assert summary["cycle_id"] == "legacy-cycle"
            assert summary["result"]["cycle_id"] == "legacy-cycle"
            assert reopened.get_all_alerts(5)[0]["cycle_id"] == "legacy-cycle"


class TestResultFieldMapping:
    """The results produced by a monitoring cycle use one set of field
    names; the database stores them under display oriented column names;
    the API returns the column names. This test pins the whole mapping so
    the dashboard can never silently lose a field to a rename."""

    @staticmethod
    def representative_results():
        """Field names exactly as run_monitoring_cycle assembles them."""
        return {
            'timestamp': '2026-01-01T00:00:00',
            'reference_size': 50, 'current_size': 10,
            'ks_statistic': 0.3, 'ks_pvalue': 0.395, 'ks_raw_pvalue': 0.395,
            'ks_adjusted_pvalue': 1.0, 'ks_significant': 0,
            'proportion_z_stat': None, 'proportion_z_pvalue': 1.0,
            'proportion_z_raw_pvalue': 1.0, 'proportion_z_adjusted_pvalue': 1.0,
            'proportion_z_significant': 0,
            'mmd_statistic': 0.0, 'mmd_pvalue': 0.98,
            'mmd_raw_pvalue': 0.98, 'mmd_adjusted_pvalue': 1.0,
            'mmd_significant': 0,
            # effect sizes: note the results dict uses ref_/cur_ prefixes
            'ref_refusal_rate': 0.0, 'cur_refusal_rate': 0.05,
            'reference_unique_responses': 12, 'current_unique_responses': 10,
            'length_shift_pct': 19.5, 'centroid_distance': 0.153,
            # paired prompt level diagnostic
            'paired_cluster_mmd_statistic': -0.027,
            'paired_cluster_mmd_pvalue': 1.0,
            'paired_cluster_prompt_pairs': 10,
            # production permutation metadata
            'mmd_permutation_unit': 'prompt_cluster',
            'paired_cluster_method': 'sampled',
            # provider cost visibility
            'provider_latency_mean_seconds': 4.2,
            'provider_latency_p50_seconds': 4.0,
            'provider_latency_p95_seconds': 5.1,
            'probe_tokens_total': 960,
            # split signal families as the monitor now emits them
            'distribution_shift_detected': 0, 'quality_change_detected': 0,
            'bh_significant_tests': [], 'overall_drift_detected': 0,
            'overall_quality_degradation': 0,
        }

    def test_effect_sizes_survive_results_to_storage_to_api(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "fresh.db"))
        storage.insert_test_results(self.representative_results())
        stored = storage.get_all_test_results(limit=1)[0]
        # Prefixed results keys land in display named columns...
        assert stored['refusal_rate_reference'] == 0.0
        assert stored['refusal_rate_current'] == 0.05
        assert stored['unique_reference'] == 12
        assert stored['unique_current'] == 10
        assert stored['length_shift_pct'] == 19.5
        assert stored['centroid_distance'] == 0.153
        assert stored['paired_cluster_prompt_pairs'] == 10
        # ... and the API field list reads exactly those columns.
        from api import compute_latest_fields
        payload = compute_latest_fields(stored)
        assert payload['refusal_rate_reference'] == 0.0
        assert payload['refusal_rate_current'] == 0.05
        assert payload['unique_reference'] == 12
        assert payload['unique_current'] == 10
        assert payload['paired_cluster_mmd_pvalue'] == 1.0
        # Production permutation metadata and provider cost fields survive.
        assert payload['mmd_permutation_unit'] == 'prompt_cluster'
        assert payload['paired_cluster_method'] == 'sampled'
        assert payload['provider_latency_mean_seconds'] == 4.2
        assert payload['provider_latency_p95_seconds'] == pytest.approx(5.1)
        assert payload['probe_tokens_total'] == 960
        # The split signals round trip: a validity flag can be on while the
        # distribution family is off, and each field reads independently.
        assert payload['distribution_shift_detected'] == 0
        assert payload['quality_change_detected'] == 0
        # A refusal rate change card computed from these fields is real data,
        # not a missing read.
        assert payload['refusal_rate_current'] - payload['refusal_rate_reference'] == pytest.approx(0.05)

    def test_split_signals_derived_from_legacy_rows(self, tmp_path):
        """Rows stored before the split existed still derive it, from the
        distributional flags only: a validity flag can never leak into the
        distribution verdict through this path."""
        storage = DriftMonitorStorage(str(tmp_path / "legacy_split.db"))
        results = self.representative_results()
        results.update({
            'ks_significant': 0, 'mmd_significant': 0,
            'proportion_z_significant': 1,
            'overall_drift_detected': 1,  # legacy "any flag" field, ignored
        })
        results.pop('distribution_shift_detected', None)
        results.pop('quality_change_detected', None)
        storage.insert_test_results(results)
        stored = storage.get_all_test_results(limit=1)[0]
        from api import compute_latest_fields
        payload = compute_latest_fields(stored)
        assert payload['distribution_shift_detected'] == 0
        assert payload['quality_change_detected'] == 1
        assert payload['drift_detected'] == 1  # legacy passthrough stays

    def test_split_signals_from_distributional_flags(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "dist_split.db"))
        results = self.representative_results()
        results.update({'ks_significant': 1, 'mmd_significant': 0,
                        'proportion_z_significant': 0})
        results.pop('distribution_shift_detected', None)
        results.pop('quality_change_detected', None)
        storage.insert_test_results(results)
        stored = storage.get_all_test_results(limit=1)[0]
        from api import compute_latest_fields
        payload = compute_latest_fields(stored)
        assert payload['distribution_shift_detected'] == 1
        assert payload['quality_change_detected'] == 0

    def test_legacy_rows_without_effect_sizes_read_as_null(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "legacy.db"))
        # Insert a row with only the pre effect size fields.
        with storage._db() as conn:
            conn.execute(
                "INSERT INTO test_results (timestamp, reference_size, current_size, "
                "ks_statistic, ks_pvalue, bh_significant_tests, overall_drift_detected) "
                "VALUES ('2025-01-01T00:00:00', 50, 10, 0.4, 0.11, '[]', 0)")
        stored = storage.get_all_test_results(limit=1)[0]
        assert stored['length_shift_pct'] is None
        assert stored['refusal_rate_reference'] is None
        assert stored['unique_reference'] is None
        assert stored['paired_cluster_mmd_statistic'] is None


class TestRunThrottle:
    """The public run throttle: one check per visitor per interval, and a
    shared daily cycle budget, both explicit 429s with Retry-After. These
    limits exist because a hosted cycle spends real provider quota."""

    @staticmethod
    def reset_state(monkeypatch):
        from fastapi.testclient import TestClient

        from api import app as api_app
        monkeypatch.setattr("api.MIN_RUN_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr("api.PUBLIC_RUN_INTERVAL_SECONDS", 600.0)
        monkeypatch.setattr("api._last_enqueue", [0.0])
        monkeypatch.setattr("api._ip_last_run", {})
        monkeypatch.setattr("api._runs", {})
        monkeypatch.setattr("api._idempotency", {})
        monkeypatch.setattr("api._daily_usage",
                            {"date": "2099-01-01", "count": 0})
        # Never hand runs to the real worker: the monitor costs an
        # embedding model load no unit test should pay.
        class BlockedQueue:
            def __init__(self):
                self.items = []
            def put_nowait(self, item):
                self.items.append(item)
            def qsize(self):
                return len(self.items)
            def get(self, *args, **kwargs):
                import time
                time.sleep(600)
        monkeypatch.setattr("api._run_queue", BlockedQueue())
        return TestClient(api_app)

    def test_second_run_by_same_ip_inside_interval_is_rejected(self, monkeypatch):
        client = self.reset_state(monkeypatch)
        first = client.post("/runs")
        assert first.status_code == 200
        second = client.post("/runs")
        assert second.status_code == 429
        assert "Retry-After" in second.headers
        assert "600" in second.headers["Retry-After"]

    def test_daily_budget_rejects_after_exhaustion(self, monkeypatch):
        monkeypatch.setattr("api.DAILY_CYCLE_BUDGET", 1)
        client = self.reset_state(monkeypatch)
        # reset_state leaves a stale date on purpose; stamp today with zero
        # used so the first acceptance consumes the single-item budget.
        from api import _daily_count_and_date
        today = _daily_count_and_date()[1]
        monkeypatch.setattr("api._daily_usage", {"date": today, "count": 0})
        # Collapse the per visitor interval so the second POST reaches the
        # budget check instead of being stopped one limit earlier.
        monkeypatch.setattr("api.PUBLIC_RUN_INTERVAL_SECONDS", 0.0)
        first = client.post("/runs")
        assert first.status_code == 200
        second = client.post("/runs")
        assert second.status_code == 429
        assert "daily cycle budget" in second.json()["detail"]
        # Retry-After points to UTC midnight, meaning it is positive and
        # bounded by a day.
        assert 0 < int(second.headers["Retry-After"]) <= 86400

    def test_rejected_runs_do_not_consume_budget(self, monkeypatch):
        client = self.reset_state(monkeypatch)
        client.post("/runs")
        rejected = client.post("/runs")
        assert rejected.status_code == 429
        from api import _daily_usage
        assert _daily_usage["count"] == 1  # only the accepted run counted

    def test_budget_counts_a_date_rolled_forward(self, monkeypatch):
        client = self.reset_state(monkeypatch)
        client.post("/runs")
        from api import _daily_usage
        # A stale date resets the counter rather than carrying yesterday's
        # usage into today.
        assert _daily_usage["count"] == 1
        _daily_usage["date"] = "1999-01-01"
        from api import _daily_count_and_date
        count, date = _daily_count_and_date()
        assert count == 0 and date != "1999-01-01"

    def test_configured_but_failed_storage_rejects_new_runs(self, monkeypatch, tmp_path):
        import storage as storage_module
        from api import _daily_usage, _last_enqueue, _run_queue, _runs
        from storage import create_storage

        def unavailable(_url):
            raise RuntimeError("postgresql://user:hunter2@db.example/monitor failed")

        monkeypatch.setattr(storage_module, "PostgresStorage", unavailable)
        fallback = create_storage(str(tmp_path / "fallback.db"),
                                  "postgresql://user:hunter2@db.example/monitor")
        client = self.reset_state(monkeypatch)
        monkeypatch.setattr("api._get_monitor", lambda: StubMonitor(fallback))

        rejected = client.post("/runs")
        assert rejected.status_code == 503
        assert "persistent storage is unavailable" in rejected.json()["detail"]
        # Nothing was enqueued, recorded, metered, or written to SQLite.
        assert _run_queue.qsize() == 0
        assert _runs == {}
        assert _last_enqueue[0] == 0.0
        assert _daily_usage["count"] == 0
        assert fallback.count_all_runs() == 0

    def test_monitor_init_failure_also_rejects_new_runs(self, monkeypatch):
        from api import _daily_usage, _run_queue, _runs

        client = self.reset_state(monkeypatch)

        def boom():
            raise RuntimeError("storage init failed")

        monkeypatch.setattr("api._get_monitor", boom)
        rejected = client.post("/runs")
        assert rejected.status_code == 503
        assert "persistent storage is unavailable" in rejected.json()["detail"]
        assert _run_queue.qsize() == 0
        assert _runs == {}
        assert _daily_usage["count"] == 0


class StubMonitor:
    """Duck typed LLMDriftMonitor for endpoint tests without adapter or
    embedding model construction."""

    def __init__(self, storage, adapter=None, adapter_error=None):
        self.storage = storage
        self.adapter = adapter
        self.adapter_error = adapter_error
        self.config = {"target": {"model_id": "gemini-3.1-flash-lite"},
                       "embeddings": {"model_name":
                                          "sentence-transformers/all-MiniLM-L6-v2"},
                       "reference": {"window_size": 120, "min_runs": 10},
                       "monitoring": {"batch_size": 40, "category_window_cycles": 3,
                                      "min_samples_per_category": 8},
                       "alerts": {"significance_threshold": 0.05,
                                  "min_consecutive_cycles": 2}}
        self.canary_prompts = {"version": "3.0.0", "prompts": [{"id": i}
            for i in range(40)]}


class TestProviderUnavailableSurface:
    def test_health_reports_not_configured_explicitly(self, monkeypatch, tmp_path):
        from api import health

        storage = DriftMonitorStorage(str(tmp_path / "stub.db"))
        monitor = StubMonitor(storage, adapter=None,
                              adapter_error=(
                                  "Gemini API key is required. Set "
                                  "GEMINI_API_KEY (e.g. in .env) or pass "
                                  "api_key explicitly."))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        payload = health()
        assert payload["status"] == "degraded"
        assert payload["provider"] == "gemini-3.1-flash-lite"
        assert payload["provider_status"] == "not_configured"
        assert "API key is required" in payload["provider_error"]

    def test_config_reports_provider_unavailable_and_policy(self, monkeypatch, tmp_path):
        from api import get_config

        storage = DriftMonitorStorage(str(tmp_path / "stub.db"))
        storage.insert_manifest({"provider": "gemini",
                                 "model_id": "gemini-3.1-flash-lite"})
        monitor = StubMonitor(storage, adapter=None, adapter_error="missing key")
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        payload = get_config()
        assert payload["provider_available"] is False
        assert payload["provider_error"] == "missing key"
        assert payload["baseline_manifest"]["provider"] == "gemini"
        assert payload["run_policy"]["daily_cycle_budget"] > 0
        # Local SQLite is explicitly writable and explicitly not durable.
        state = payload["storage"]
        assert state["backend"] == "sqlite"
        assert state["durable"] is False
        assert state["fallback"] is False
        assert state["accepts_writes"] is True
        assert state["new_checks_enabled"] is True
        # No key material of any kind appears in the config endpoint.
        assert "GEMINI_API_KEY" not in str(payload)
        assert "api_key" not in str(payload).lower()


class TestStorageStateSurface:
    """Storage identity and write capability share one shape in /config,
    /health and the run gate, so the dashboard cannot mistake a read-only
    fallback for a healthy writable backend."""

    @staticmethod
    def _fallback_storage(tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "fallback.db"))
        storage.backend = "sqlite-fallback"
        storage.is_durable = False
        storage.accepts_writes = False
        storage.storage_error = ("connection failed api_key=hunter2 "
                                 "postgresql://user:s3cr3t@db.example/monitor")
        return storage

    def test_config_names_the_read_only_fallback_and_redacts_its_error(
            self, monkeypatch, tmp_path):
        from api import get_config, invalidate_read_cache

        storage = self._fallback_storage(tmp_path)
        monitor = StubMonitor(storage, adapter=None, adapter_error=None)
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        invalidate_read_cache()

        state = get_config()["storage"]
        assert state["backend"] == "sqlite-fallback"
        assert state["durable"] is False
        assert state["fallback"] is True
        assert state["accepts_writes"] is False
        assert state["new_checks_enabled"] is False
        assert "read-only" in state["note"]
        assert "hunter2" not in str(state)
        assert "s3cr3t" not in str(state)

    def test_health_matches_config_when_the_db_probe_fails(
            self, monkeypatch, tmp_path):
        from api import health

        storage = self._fallback_storage(tmp_path)

        def broken_count():
            raise RuntimeError("disk I/O error")

        storage.count_successful_runs = broken_count
        monitor = StubMonitor(storage, adapter=None, adapter_error=None)
        monkeypatch.setattr("api._get_storage", lambda: storage)
        monkeypatch.setattr("api._get_monitor", lambda: monitor)

        payload = health(probe=False)
        assert payload["database"] == "unavailable"
        assert payload["storage_backend"] == "sqlite-fallback"
        assert payload["storage"]["backend"] == "sqlite-fallback"
        assert payload["storage"]["new_checks_enabled"] is False
        assert "hunter2" not in str(payload)
        assert "s3cr3t" not in str(payload)


class TestComparisonRedaction:
    """Safety boundary prompts are displayed as labels only; their exact
    wording and the answers to them never leave the API."""

    @staticmethod
    def build_monitor(tmp_path):
        from monitor import LLMDriftMonitor

        storage = DriftMonitorStorage(str(tmp_path / "cmp.db"))
        monitor = LLMDriftMonitor.__new__(LLMDriftMonitor)
        monitor.storage = storage
        monitor.config = {"monitoring": {"batch_size": 2},
                          "reference": {"window_size": 2}}
        monitor.canary_prompts = {
            "version": "3.0.0",
            "prompts": [
                {"id": "fact_001", "category": "factual",
                 "text": "What is the capital of France?",
                 "public_label": "Capital of France",
                 "redact_prompt": False},
                {"id": "refu_025", "category": "safety_boundary",
                 "text": "SENSITIVE REQUEST WORDING SENTINEL",
                 "public_label": "Disallowed synthesis request, details withheld",
                 "redact_prompt": True},
            ],
        }
        return monitor

    def seed_cycles(self, monitor, cycles):
        for i in range(cycles):
            for prompt in monitor.canary_prompts["prompts"]:
                monitor.storage.insert_probe_run(
                    timestamp=f"2026-01-0{i + 1}T00:00:00",
                    prompt_version="3.0.0",
                    prompt_text=prompt["text"],
                    model_id="gemini-3.1-flash-lite",
                    response_text=f"answer {i}",
                    response_length=20 + i,
                    is_valid=True, run_status="success",
                    adapter_type="gemini",
                    prompt_id=prompt["id"],
                    category=prompt["category"],
                    cycle_id=f"cycle{i}",
                )

    def test_redacted_prompt_never_exposes_wording(self, tmp_path):
        monitor = self.build_monitor(tmp_path)
        self.seed_cycles(monitor, 3)  # 2 reference cycles + 1 current
        payload = monitor.get_window_comparison()
        assert payload["ready"] is True
        rows = {r["prompt_id"]: r for r in payload["rows"]}
        safe_row = rows["fact_001"]
        assert safe_row["prompt_text"] == "What is the capital of France?"
        assert safe_row["prompt_redacted"] is False
        assert safe_row["reference"]["text"] is not None

        red_row = rows["refu_025"]
        assert red_row["prompt_redacted"] is True
        assert red_row["label"] == "Disallowed synthesis request, details withheld"
        assert "SENSITIVE" not in str(red_row)
        assert "SENTINEL" not in str(red_row)
        assert red_row["prompt_text"] == "(withheld: safety boundary prompt)"
        # The answers are withheld the same way; the metadata stays visible.
        assert red_row["reference"]["text"] == "(withheld: safety boundary prompt)"
        assert red_row["reference"]["length"] is not None
        assert red_row["reference"]["is_valid"] is not None
        assert red_row["current"]["text"].startswith("(withheld")

    def test_manifest_roundtrip(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "manifest.db"))
        assert storage.get_manifest() == {}
        storage.insert_manifest({"provider": "gemini",
                                 "model_id": "gemini-3.1-flash-lite",
                                 "panel_version": "3.0.0"})
        storage.insert_manifest({"provider": "gemini"})  # update, not duplicate
        manifest = storage.get_manifest()
        assert manifest == {"provider": "gemini",
                            "model_id": "gemini-3.1-flash-lite",
                            "panel_version": "3.0.0"}


class TestRunRecordBound:
    """Finding: _runs accumulated every polled run forever; completed
    records now prune oldest-first once the bound is exceeded."""

    def test_record_run_prunes_oldest(self, monkeypatch):
        from api import MAX_RUN_RECORDS, _record_run, _runs
        _runs.clear()
        try:
            for i in range(MAX_RUN_RECORDS + 25):
                _record_run(f"run-{i}", {"status": "completed"})
            assert len(_runs) == MAX_RUN_RECORDS
            # Oldest records went first; the newest is always retained.
            assert f"run-{MAX_RUN_RECORDS + 24}" in _runs
            assert "run-0" not in _runs
        finally:
            _runs.clear()

    def test_record_run_keeps_a_live_running_record(self, monkeypatch):
        """A running record accepted now is younger than any completed one
        pruned later, but a second running record can never evict it while
        the bound has headroom (the demo budget is far below MAX)."""
        from api import MAX_RUN_RECORDS, _record_run, _runs
        _runs.clear()
        try:
            _record_run("live", {"status": "running"})
            for i in range(MAX_RUN_RECORDS):
                _record_run(f"done-{i}", {"status": "completed"})
            assert "live" in _runs  # never over the bound, never pruned
            _record_run("done-x", {"status": "completed"})  # hits bound
            assert "live" in _runs
        finally:
            _runs.clear()


class TestCycleActivity:
    """Recent Activity is cycle level: every stored cycle appears once, and
    its detector result is attached by exact cycle id, never by timestamp."""

    @staticmethod
    def seed_cycle(storage, cycle_id, count, timestamp, failed=0):
        for i in range(count):
            ok = i >= failed
            storage.insert_probe_run(
                timestamp=timestamp,
                prompt_version="3.0.0",
                prompt_text=f"prompt {i}",
                model_id="gemini-3.1-flash-lite",
                response_text=f"answer {cycle_id} {i}",
                response_length=20 + i,
                is_valid=ok,
                run_status="success" if ok else "failed",
                adapter_type="gemini",
                prompt_id=f"p{i}",
                category="general",
                cycle_id=cycle_id,
            )

    def build(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "cycles.db"))
        self.seed_cycle(storage, "c1", 3, "2026-01-01T00:00:00")
        self.seed_cycle(storage, "c2", 3, "2026-01-02T00:00:00")
        self.seed_cycle(storage, "c3", 2, "2026-01-03T00:00:00", failed=1)
        result = TestResultFieldMapping.representative_results()
        result["cycle_id"] = "c1"
        # Deliberately unrelated to the cycle's own clock: the link is an id.
        result["timestamp"] = "2099-12-31T23:59:59"
        storage.insert_test_results(result)
        return storage

    def test_cycles_are_grouped_with_exact_result_links(self, tmp_path):
        storage = self.build(tmp_path)
        summaries = {c["cycle_id"]: c for c in storage.get_cycle_summaries(10)}
        assert set(summaries) == {"c1", "c2", "c3"}
        assert summaries["c1"]["result"]["cycle_id"] == "c1"
        assert summaries["c2"]["result"] is None
        assert summaries["c3"]["probe_count"] == 2
        assert summaries["c3"]["failed_count"] == 1
        assert summaries["c1"]["cycle_number"] == 1
        assert summaries["c3"]["cycle_number"] == 3

    def test_read_endpoints_agree_on_stored_counts(self, tmp_path, monkeypatch):
        import api

        storage = self.build(tmp_path)  # 8 probes, 1 result, 3 cycles
        monitor = StubMonitor(storage)
        monitor.config["monitoring"]["batch_size"] = 3
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        monkeypatch.setattr("api._get_storage", lambda: storage)
        api.invalidate_read_cache()

        stats = api.get_stats()
        runs = api.list_runs(limit=100)
        results = api.get_drift_results(limit=100)
        cycles = api.get_cycles(limit=100)
        assert stats["probe_runs"] == len(runs) == 8
        assert stats["test_results"] == len(results) == 1
        assert stats["cycles"] == len(cycles) == 3
        assert [run.id for run in runs] == [8, 7, 6, 5, 4, 3, 2, 1]
        assert [cycle["cycle_number"] for cycle in cycles] == [3, 2, 1]
        assert [cycle["cycle_id"] for cycle in cycles] == ["c3", "c2", "c1"]

    def test_diagnostics_compare_probes_results_and_cycles(self, tmp_path):
        storage = self.build(tmp_path)
        counts = storage.get_diagnostics(expected_probes=3)
        assert counts["probe_runs"] == 8
        assert counts["cycles"] == 3
        assert counts["test_results"] == 1
        assert counts["evaluated_cycles"] == 1
        assert counts["complete_cycles"] == 2
        assert counts["awaiting_evaluation_cycles"] == 1
        assert counts["failed_cycles"] == 1
        assert counts["test_results_without_cycle"] == 0
        assert counts["probe_runs_without_cycle"] == 0

    def test_cycles_endpoint_labels_awaiting_and_interrupted(self, tmp_path, monkeypatch):
        import api

        storage = self.build(tmp_path)
        monitor = StubMonitor(storage)
        monitor.config["monitoring"]["batch_size"] = 3
        # This fixture is not a reference baseline: keep baseline detection off
        # so the status logic under test is interruption/awaiting-evaluation.
        monitor.config["reference"]["window_size"] = 0
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        api.invalidate_read_cache()
        payload = {c["cycle_id"]: c for c in api.get_cycles(limit=10)}
        assert payload["c1"]["status"] == "evaluated"
        assert payload["c2"]["status"] == "awaiting_evaluation"
        assert payload["c3"]["status"] == "interrupted"
        assert payload["c1"]["probe_count"] == 3
        assert payload["c1"]["result"]["cycle_id"] == "c1"


class TestBaselineRoles:
    """The committed Gemini baseline is three reference cycles plus one
    evaluated cycle. Role and empty-output facts come from stored records."""

    @staticmethod
    def committed_seed_copy(tmp_path):
        import shutil
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "seed_gemini.db")
        target = str(tmp_path / "seed_copy.db")
        shutil.copyfile(source, target)
        return source, DriftMonitorStorage(target)

    def test_three_baseline_cycles_and_one_evaluated_cycle(self, tmp_path, monkeypatch):
        import api as api_module

        _, storage = self.committed_seed_copy(tmp_path)
        monitor = StubMonitor(storage)
        monitor.config["reference"]["window_size"] = 120
        monitor.config["monitoring"]["batch_size"] = 40
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        api_module.invalidate_read_cache()
        cycles = {c["cycle_number"]: c for c in api_module.get_cycles(limit=10)}
        assert {n: c["status"] for n, c in cycles.items()} == {
            1: "reference_baseline", 2: "reference_baseline",
            3: "reference_baseline", 4: "evaluated",
        }
        assert cycles[1]["empty_count"] == 1
        assert cycles[1]["failed_count"] == 0
        assert cycles[4]["result"] is not None

    def test_baseline_cycles_are_not_awaiting_evaluation(self, tmp_path):
        _, storage = self.committed_seed_copy(tmp_path)
        counts = storage.get_diagnostics(expected_probes=40, reference_window=120)
        assert counts["probe_runs"] == 160
        assert counts["cycles"] == 4
        assert counts["reference_baseline_cycles"] == 3
        assert counts["evaluated_cycles"] == 1
        assert counts["awaiting_evaluation_cycles"] == 0
        assert counts["failed_cycles"] == 0
        assert counts["empty_probe_runs"] == 3
        assert counts["failed_probe_runs"] == 0

    def test_no_reference_window_keeps_legacy_awaiting_count(self, tmp_path):
        _, storage = self.committed_seed_copy(tmp_path)
        counts = storage.get_diagnostics(expected_probes=40, reference_window=None)
        assert counts["reference_baseline_cycles"] == 0
        assert counts["awaiting_evaluation_cycles"] == 3


class TestProbeOutcomes:
    """Empty output and provider failure are different stored outcomes."""

    def test_legacy_empty_rows_are_relabelled_once(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        raw = sqlite3.connect(path)
        raw.execute("""CREATE TABLE probe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            prompt_version TEXT NOT NULL, prompt_text TEXT NOT NULL,
            model_id TEXT NOT NULL, response_text TEXT NOT NULL,
            response_length INTEGER, is_valid INTEGER,
            run_status TEXT NOT NULL, adapter_type TEXT NOT NULL)""")
        raw.execute("""INSERT INTO probe_runs
            (timestamp, prompt_version, prompt_text, model_id, response_text,
             response_length, is_valid, run_status, adapter_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("t", "3", "p", "m", "", 0, 0, "failed", "gemini"))
        raw.commit()
        raw.close()

        storage = DriftMonitorStorage(path)
        with storage._db() as conn:
            status = conn.execute(
                "SELECT run_status FROM probe_runs").fetchone()[0]
        assert status == "empty"

        # A genuine later failure must not be rewritten by a rerun.
        storage.insert_probe_run(
            timestamp="t2", prompt_version="3", prompt_text="p", model_id="m",
            response_text="", response_length=0, is_valid=False,
            run_status="failed", adapter_type="gemini")
        reopened = DriftMonitorStorage(path)
        with reopened._db() as conn:
            statuses = {row[0] for row in conn.execute(
                "SELECT run_status FROM probe_runs").fetchall()}
        assert statuses == {"empty", "failed"}

    def test_import_maps_legacy_empty_without_mutating_source(self, tmp_path):
        from storage import import_baseline

        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "seed_gemini.db")
        storage = DriftMonitorStorage(str(tmp_path / "import_target.db"))
        before = sqlite3.connect(source).execute(
            "SELECT run_status FROM probe_runs WHERE id = 18").fetchone()[0]
        assert before == "failed"
        summary = import_baseline(storage, source)
        assert summary["imported_runs"] == 160
        with storage._db() as conn:
            statuses = {row[0] for row in conn.execute(
                "SELECT DISTINCT run_status FROM probe_runs").fetchall()}
        assert "empty" in statuses and "failed" not in statuses
        after = sqlite3.connect(source).execute(
            "SELECT run_status FROM probe_runs WHERE id = 18").fetchone()[0]
        assert after == before

        # A second import must not duplicate the baseline.
        again = import_baseline(storage, source)
        assert again["imported_cycles"] == 0
        assert again["imported_runs"] == 0
        counts = storage.get_diagnostics(expected_probes=40, reference_window=120)
        assert counts["probe_runs"] == 160


class TestCycleIdMigration:
    def test_legacy_result_is_linked_to_the_cycle_it_followed(self, tmp_path):
        path = tmp_path / "legacy-links.db"
        storage = DriftMonitorStorage(str(path))
        TestCycleActivity.seed_cycle(storage, "old-cycle", 3,
                                     "2026-02-01T00:00:00")
        result = TestResultFieldMapping.representative_results()
        result["cycle_id"] = None
        result["timestamp"] = "2026-02-01T00:05:00"
        storage.insert_test_results(result)
        # Simulate a database written before the cycle link existed.
        conn = sqlite3.connect(path)
        conn.execute("UPDATE test_results SET cycle_id = NULL")
        conn.commit()
        conn.close()

        reopened = DriftMonitorStorage(str(path))
        stored = reopened.get_all_test_results(limit=1)[0]
        assert stored["cycle_id"] == "old-cycle"
        assert reopened.get_diagnostics(3)["test_results_without_cycle"] == 0


class TestStorageSelection:
    """Local SQLite stays writable; a configured failed backend is read-only."""

    def test_unset_database_url_selects_writable_local_sqlite(self, monkeypatch, tmp_path):
        from storage import create_storage

        monkeypatch.delenv("DATABASE_URL", raising=False)
        storage = create_storage(str(tmp_path / "local.db"), None)
        assert storage.backend == "sqlite"
        assert storage.is_durable is False
        assert storage.storage_error is None
        assert storage.info()["accepts_writes"] is True
        storage.insert_probe_run(
            timestamp="2026-03-01T00:00:00", prompt_version="3.0.0",
            prompt_text="p", model_id="m", response_text="r",
            response_length=1, is_valid=True, run_status="success",
            adapter_type="fake", prompt_id="p1", category="general",
            cycle_id="local-1")
        assert storage.count_all_runs() == 1

    def test_failed_postgres_fallback_refuses_every_monitoring_write(
            self, monkeypatch, tmp_path):
        import storage as storage_module
        from storage import create_storage

        def unavailable(_url):
            raise RuntimeError("connection failed with api_key=hunter2")

        monkeypatch.setattr(storage_module, "PostgresStorage", unavailable)
        storage = create_storage(str(tmp_path / "fallback.db"), "postgresql://db")
        info = storage.info()
        assert storage.backend == "sqlite-fallback"
        assert storage.is_durable is False
        assert storage.accepts_writes is False
        assert info["accepts_writes"] is False
        assert "hunter2" not in str(info)  # startup logs stay credential-free

        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_probe_run(
                timestamp="2026-03-01T00:00:00", prompt_version="3.0.0",
                prompt_text="p", model_id="m", response_text="r",
                response_length=1, is_valid=True, run_status="success",
                adapter_type="fake")
        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_response_features(1, 1, True)
        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_embedding(1, [0.1])
        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_manifest({"provider": "gemini"})
        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_test_results(
                TestResultFieldMapping.representative_results())
        with pytest.raises(RuntimeError, match="new monitoring writes"):
            storage.insert_alert({
                "timestamp": "2026-03-01T00:00:00", "alert_type": "drift",
                "severity": "warning", "message": "blocked"})
        assert storage.count_all_runs() == 0
        assert storage.get_manifest() == {}

    def test_fallback_does_not_import_a_baseline(self, monkeypatch, tmp_path):
        import monitor as monitor_module
        from monitor import LLMDriftMonitor
        from types import SimpleNamespace

        calls = []
        monkeypatch.setattr(monitor_module, "import_baseline",
                            lambda target, source: calls.append((target, source)))
        storage = DriftMonitorStorage(str(tmp_path / "fallback.db"))
        storage.backend = "sqlite-fallback"
        storage.accepts_writes = False
        LLMDriftMonitor._ensure_baseline_import(SimpleNamespace(storage=storage))
        assert calls == []

    def test_postgres_imports_the_baseline_once(self, monkeypatch, tmp_path):
        import monitor as monitor_module
        from monitor import LLMDriftMonitor
        from types import SimpleNamespace

        seed = str(tmp_path / "seed.db")
        calls = []
        monkeypatch.setenv("DRIFT_MONITOR_SEED_DB", seed)
        monkeypatch.setattr(monitor_module, "import_baseline",
                            lambda target, source: calls.append((target, source)))
        storage = DriftMonitorStorage(str(tmp_path / "postgres.db"))
        storage.backend = "postgres"
        LLMDriftMonitor._ensure_baseline_import(SimpleNamespace(storage=storage))
        assert calls == [(storage, seed)]


class TestPostgresConnectionRecovery:
    """Mocked pool-level failures from the observed Neon SSL close."""

    class FakeCursor:
        def __init__(self, connection):
            self._connection = connection
            self._description = [("one",)]

        def execute(self, sql, params=None):
            import psycopg

            if not self._connection.alive:
                raise psycopg.OperationalError(
                    "consuming input failed: SSL connection has been closed unexpectedly")
            if self._connection.fail_query and sql.strip():
                self._connection.fail_query = False
                raise psycopg.OperationalError(
                    "consuming input failed: SSL connection has been closed unexpectedly")
            self._description = [("one",)]

        @property
        def description(self):
            return self._description

        def fetchone(self):
            return (1,)

        def fetchall(self):
            return [(1,)]

    class FakeConnection:
        def __init__(self, alive=True, fail_query=False):
            self.alive = alive
            self.fail_query = fail_query
            self.autocommit = False

        def execute(self, sql, params=None):
            cursor = self.cursor()
            cursor.execute(sql, params)
            return cursor

        def cursor(self):
            return TestPostgresConnectionRecovery.FakeCursor(self)

        def commit(self):
            return None

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.checkouts = 0
            self.fail_first_query = False
            self.stale_first = True

        def connection(self, timeout=None):
            @contextmanager
            def checkout():
                # The real pool retries checked-out connections with its
                # backoff policy until one passes `check` or the timeout ends.
                for _ in range(4):
                    self.checkouts += 1
                    conn = TestPostgresConnectionRecovery.FakeConnection(
                        alive=not (self.stale_first and self.checkouts == 1),
                        fail_query=self.fail_first_query and self.checkouts == 1)
                    check = self.kwargs.get("check")
                    try:
                        if check:
                            check(conn)
                    except Exception:
                        continue
                    yield conn
                    return
                raise RuntimeError("no checked-out connection passed the check")

            return checkout()

        def close(self):
            self.closed = True

    @staticmethod
    def _storage(monkeypatch, fail_first_query=False):
        import psycopg_pool
        import storage as storage_module
        from psycopg_pool import ConnectionPool

        # Capture the real API before replacing the constructor under test.
        check_callback = ConnectionPool.check_connection
        monkeypatch.setattr(TestPostgresConnectionRecovery.FakePool,
                            "check_connection", staticmethod(check_callback),
                            raising=False)
        monkeypatch.setattr(psycopg_pool, "ConnectionPool",
                            TestPostgresConnectionRecovery.FakePool)
        monkeypatch.setattr(storage_module.PostgresStorage, "_init_db",
                            lambda self: None)
        storage = storage_module.PostgresStorage("postgresql://db.example/monitor")
        if fail_first_query:
            storage._pool.fail_first_query = True
            storage._pool.stale_first = False
        return storage, check_callback

    def test_stale_ssl_connection_is_checked_and_replaced(self, monkeypatch):
        storage, check_callback = self._storage(monkeypatch)
        kwargs = storage._pool.kwargs

        assert kwargs["check"] is check_callback
        assert kwargs["max_size"] == 2
        assert kwargs["max_lifetime"] <= 900
        assert kwargs["max_idle"] <= 600
        assert kwargs["kwargs"]["connect_timeout"] >= 8

        storage.check_connection()
        # The first warm socket failed the checkout callback; the second was
        # used for SELECT 1. No SQL was attempted on the dead connection.
        assert storage._pool.checkouts == 2
        assert storage.is_reachable is True
        assert storage.storage_error is None

    def test_query_interruption_marks_unavailable_then_recovers(self, monkeypatch):
        storage, _ = self._storage(monkeypatch, fail_first_query=True)

        with pytest.raises(Exception, match="SSL connection has been closed"):
            storage.count_all_runs()
        assert storage.is_reachable is False
        assert "SSL connection has been closed" in storage.storage_error

        storage.check_connection()
        assert storage.is_reachable is True
        assert storage.storage_error is None
        assert storage._pool.checkouts >= 2

    def test_close_is_idempotent(self, monkeypatch):
        storage, _ = self._storage(monkeypatch)
        assert storage._pool.closed is False
        storage.close()
        storage.close()
        assert storage._pool.closed is True

    def test_local_sqlite_check_remains_writable(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "sqlite-check.db"))
        assert storage.check_connection() is None
        assert storage.is_reachable is True
        assert storage.accepts_writes is True
        assert storage.info()["reachable"] is True


class TestTransientStorageFailureSurface:
    """A configured durable backend can be live-unavailable."""

    class Adapter:
        model_id = "gemini-3.1-flash-lite"

        def ping(self):
            return True

    class UnreachablePostgres(DriftMonitorStorage):
        backend = "postgres"
        is_durable = True

        def check_connection(self):
            raise RuntimeError(
                "consuming input failed: SSL connection has been closed unexpectedly")

    @staticmethod
    def _monitor(tmp_path, storage):
        return StubMonitor(storage, adapter=TestTransientStorageFailureSurface.Adapter())

    def test_health_reports_live_database_failure(self, monkeypatch, tmp_path):
        from api import health

        storage = self.UnreachablePostgres(str(tmp_path / "unreachable.db"))
        monkeypatch.setattr("api._get_storage", lambda: storage)
        monkeypatch.setattr("api._get_monitor",
                            lambda: self._monitor(tmp_path, storage))
        monkeypatch.setattr("api._provider_health",
                            {"at": 0.0, "ok": None, "status": None, "error": None})

        payload = health(probe=False)
        assert payload["database"] == "unavailable"
        assert payload["storage_backend"] == "postgres"
        assert payload["storage_durable"] is True
        assert payload["storage"]["reachable"] is False
        assert payload["storage"]["new_checks_enabled"] is False
        assert payload["diagnostics"]["db_ready"] is False
        assert "SSL connection has been closed" in payload["db_error"]

    def test_run_is_rejected_before_queueing_without_sqlite_fallback(
            self, monkeypatch, tmp_path):
        from api import _daily_usage, _last_enqueue, _run_queue, _runs

        storage = self.UnreachablePostgres(str(tmp_path / "unreachable-runs.db"))
        client = TestRunThrottle.reset_state(monkeypatch)
        monkeypatch.setattr("api._get_monitor",
                            lambda: self._monitor(tmp_path, storage))

        rejected = client.post("/runs")
        assert rejected.status_code == 503
        assert "persistent storage is unavailable" in rejected.json()["detail"]
        assert _run_queue.qsize() == 0
        assert _runs == {}
        assert _last_enqueue[0] == 0.0
        assert _daily_usage["count"] == 0
        assert storage.count_all_runs() == 0
        assert storage.get_manifest() == {}


class TestDurablePersistence:
    def test_sqlite_records_survive_a_process_restart(self, tmp_path):
        db_path = tmp_path / "restart.db"
        result = TestResultFieldMapping.representative_results()
        result["cycle_id"] = "persist-1"
        script = (
            "import json\n"
            "from storage import DriftMonitorStorage\n"
            f"s = DriftMonitorStorage({str(db_path)!r})\n"
            "s.insert_probe_run(timestamp='2026-03-01T00:00:00', "
            "prompt_version='3.0.0', prompt_text='p', model_id='m', "
            "response_text='r', response_length=1, is_valid=True, "
            "run_status='success', adapter_type='gemini', prompt_id='p1', "
            "category='general', cycle_id='persist-1')\n"
            f"s.insert_test_results({result!r})\n"
            "print(json.dumps({'runs': s.count_all_runs(), "
            "'results': s.count_test_results()}))\n"
        )
        insert = subprocess.run([sys.executable, "-c", script],
                                cwd=os.path.dirname(os.path.dirname(__file__)),
                                capture_output=True, text=True, check=True)
        assert json.loads(insert.stdout) == {"runs": 1, "results": 1}

        read_script = (
            "import json\n"
            "from storage import DriftMonitorStorage\n"
            f"s = DriftMonitorStorage({str(db_path)!r})\n"
            "print(json.dumps({'runs': s.count_all_runs(), "
            "'results': s.count_test_results(), "
            "'cycles': [c['cycle_id'] for c in s.get_cycle_summaries(5)]}))\n"
        )
        read = subprocess.run([sys.executable, "-c", read_script],
                              cwd=os.path.dirname(os.path.dirname(__file__)),
                              capture_output=True, text=True, check=True)
        payload = json.loads(read.stdout)
        assert payload["runs"] == 1
        assert payload["results"] == 1
        assert payload["cycles"] == ["persist-1"]

@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to a dedicated disposable database for PostgreSQL tests")
class TestPostgresIntegration:
    """Destructive integration checks.

    These tests clear tables and, in the migration case, recreate a legacy
    schema. They must only be pointed at a disposable TEST_DATABASE_URL, never
    at a production or shared database.
    """

    @staticmethod
    def _storage():
        from storage import create_storage
        return create_storage("unused.db", os.environ["TEST_DATABASE_URL"])

    @staticmethod
    def _reset_schema():
        """Drop the disposable integration tables so the next run starts modern."""
        import psycopg

        connection = psycopg.connect(os.environ["TEST_DATABASE_URL"],
                                     autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    DROP TABLE IF EXISTS embeddings, response_features,
                    test_results, alerts, probe_runs, baseline_manifest,
                    schema_migrations CASCADE
                """)
        finally:
            connection.close()

    @staticmethod
    def _seed_cycle(storage, cycle_id):
        run_id = storage.insert_probe_run(
            timestamp="2026-03-01T00:00:00", prompt_version="3.0.0",
            prompt_text="p", model_id="m", response_text="r",
            response_length=7, is_valid=True, run_status="success",
            adapter_type="gemini", prompt_id="p1", category="general",
            cycle_id=cycle_id)
        storage.insert_response_features(
            run_id, 7, True, is_refusal=0, repetition_ratio=0.1,
            token_count=7, has_code_block=0)
        storage.insert_embedding(run_id, [0.1, 0.2, 0.3])
        result = TestResultFieldMapping.representative_results()
        result.update({"cycle_id": cycle_id,
                       "timestamp": "2026-03-01T00:01:00"})
        storage.insert_test_results(result)
        storage.insert_alert({
            "timestamp": "2026-03-01T00:02:00", "cycle_id": cycle_id,
            "alert_type": "drift", "severity": "warning", "message": "persisted alert"})
        storage.insert_manifest({"provider": "test", "model_id": "test-model"})

    def test_records_survive_reconnect_with_cycle_links(self):
        first = self._storage()
        second = None
        first.clear_all()
        try:
            cycle_id = "pg-persist-test"
            self._seed_cycle(first, cycle_id)
            second = self._storage()

            assert second.backend == "postgres"
            assert second.is_durable is True
            assert second.count_all_runs() == 1
            assert second.count_test_results() == 1
            assert second.count_alerts() == 1
            assert second.get_manifest()["model_id"] == "test-model"
            assert cycle_id in second.get_cycle_ids()

            run_id = second.get_all_runs(limit=1)[0]["id"]
            assert second.get_features_for_runs([run_id])[0]["token_count"] == 7
            assert second.get_embeddings_for_runs([run_id]) == [[0.1, 0.2, 0.3]]
            summary = second.get_cycle_summaries(5)[0]
            assert summary["cycle_id"] == cycle_id
            assert summary["probe_count"] == 1
            assert summary["result"]["cycle_id"] == cycle_id
            assert second.get_all_alerts(5)[0]["cycle_id"] == cycle_id
        finally:
            if second is not None:
                second.clear_all()
                second.close()
            first.close()

    def test_live_check_recovers_after_terminated_connection(self):
        """Exercise the checkout check against the disposable database only."""
        import psycopg

        url = os.environ["TEST_DATABASE_URL"]
        storage = self._storage()
        try:
            storage.check_connection()
            assert storage.is_reachable is True
            with storage._pool.connection() as connection:
                backend_pid = connection.execute(
                    "SELECT pg_backend_pid()").fetchone()[0]
            admin = psycopg.connect(url, autocommit=True)
            try:
                admin.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
            finally:
                admin.close()

            # The checked-out socket is now dead; the next live check must
            # discard it and obtain a fresh one instead of reporting success.
            storage.check_connection()
            assert storage.is_reachable is True
            assert storage.storage_error is None
            with storage._db() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            storage.close()

    def test_baseline_import_is_idempotent(self):
        from storage import import_baseline

        storage = self._storage()
        storage.clear_all()
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "seed_gemini.db")
        try:
            first = import_baseline(storage, source)
            assert first["imported_cycles"] == 4
            assert first["imported_runs"] == 160
            counts_after_first = (storage.count_all_runs(),
                                  storage.count_test_results(),
                                  len(storage.get_cycle_summaries(20)))
            manifest = storage.get_manifest()

            second = import_baseline(storage, source)
            assert second["imported_cycles"] == 0
            assert second["imported_runs"] == 0
            assert second["imported_test_results"] == 0
            assert (storage.count_all_runs(), storage.count_test_results(),
                    len(storage.get_cycle_summaries(20))) == counts_after_first
            assert storage.get_manifest() == manifest
        finally:
            storage.clear_all()
            storage.close()

    def test_legacy_schema_migrates_additively_without_losing_rows(self):
        import psycopg

        url = os.environ["TEST_DATABASE_URL"]
        self._reset_schema()
        connection = psycopg.connect(url, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE probe_runs (
                        id BIGSERIAL PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        prompt_text TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        response_text TEXT NOT NULL,
                        response_length INTEGER,
                        is_valid INTEGER,
                        run_status TEXT NOT NULL,
                        adapter_type TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    INSERT INTO probe_runs (
                        timestamp, prompt_version, prompt_text, model_id,
                        response_text, response_length, is_valid, run_status,
                        adapter_type)
                    VALUES ('2026-03-01T00:00:00', '3.0.0', 'p', 'm',
                            'r', 1, 1, 'success', 'fake')
                """)
                cursor.execute("""
                    CREATE TABLE response_features (
                        id BIGSERIAL PRIMARY KEY,
                        run_id BIGINT NOT NULL,
                        response_length INTEGER,
                        is_valid INTEGER
                    )
                """)
                cursor.execute("""
                    CREATE TABLE test_results (
                        id BIGSERIAL PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        reference_size INTEGER,
                        current_size INTEGER,
                        ks_pvalue DOUBLE PRECISION,
                        bh_significant_tests TEXT
                    )
                """)
                cursor.execute("""
                    INSERT INTO test_results (timestamp, reference_size,
                        current_size, ks_pvalue, bh_significant_tests)
                    VALUES ('2026-03-01T00:01:00', 50, 10, 0.4, '[]')
                """)
                cursor.execute("""
                    CREATE TABLE alerts (
                        id BIGSERIAL PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        alert_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    INSERT INTO alerts (timestamp, alert_type, severity, message)
                    VALUES ('2026-03-01T00:02:00', 'drift', 'warning',
                            'legacy alert')
                """)
        finally:
            connection.close()

        storage = self._storage()
        try:
            assert storage.backend == "postgres"
            assert storage.count_all_runs() == 1
            assert storage.get_all_runs(limit=1)[0]["response_text"] == "r"
            result = storage.get_all_test_results(limit=1)[0]
            assert result["reference_size"] == 50
            assert result["cycle_id"] is None
            alert = storage.get_all_alerts(limit=1)[0]
            assert alert["message"] == "legacy alert"
            assert alert["cycle_id"] is None
            run_id = storage.get_all_runs(limit=1)[0]["id"]
            assert storage.get_features_for_runs([run_id])[0]["token_count"] is None

            reopened = self._storage()
            try:
                assert reopened.count_all_runs() == 1
                assert reopened.count_test_results() == 1
                assert reopened.count_alerts() == 1
            finally:
                reopened.close()
        finally:
            try:
                storage.close()
            finally:
                self._reset_schema()


class TestBaselineImport:
    def test_existing_baseline_manifest_is_never_overwritten(self, tmp_path):
        from storage import import_baseline

        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "seed_gemini.db")
        target = DriftMonitorStorage(str(tmp_path / "protected.db"))
        target.insert_manifest({"collected_at": "1999-01-01T00:00:00"})
        import_baseline(target, source)
        assert target.get_manifest() == {"collected_at": "1999-01-01T00:00:00"}
        assert target.count_all_runs() == 160

    def test_seed_import_is_idempotent_and_keeps_the_baseline(self, tmp_path):
        from storage import import_baseline

        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "data", "seed_gemini.db")
        target = DriftMonitorStorage(str(tmp_path / "seed-copy.db"))
        first = import_baseline(target, source)
        assert first["imported_cycles"] == 4
        assert first["imported_runs"] == 160
        assert first["imported_test_results"] == 1
        runs_after_first = target.count_all_runs()

        second = import_baseline(target, source)
        assert second["imported_cycles"] == 0
        assert second["imported_runs"] == 0
        assert target.count_all_runs() == runs_after_first == 160
        assert len(target.get_cycle_summaries(20)) == 4
        assert target.count_test_results() == 1
        counts = target.get_diagnostics(expected_probes=40)
        assert counts["test_results_without_cycle"] == 0
        assert counts["cycles"] == 4
        manifest = target.get_manifest()
        assert manifest.get("provider") == "gemini"
        assert manifest.get("model_id") == "gemini-3.1-flash-lite"


class TestStartupLogging:
    """The startup identity line must be visible and never carry secrets."""

    def test_storage_ready_names_absolute_path_and_counts(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "startup.db"))
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append((record.levelno, record.getMessage()))

        package_logger = logging.getLogger("llm_drift_monitor")
        handler = Capture()
        package_logger.addHandler(handler)
        try:
            storage.log_startup_status(40)
        finally:
            package_logger.removeHandler(handler)

        ready = [json.loads(message) for level, message in records
                 if level == logging.INFO and '"storage_ready"' in message]
        assert len(ready) == 1
        payload = ready[0]
        assert payload["location"] == str((tmp_path / "startup.db").resolve())
        assert payload["backend"] == "sqlite"
        assert payload["durable"] is False
        assert payload["counts"]["probe_runs"] == 0
        assert payload["counts"]["expected_probes_per_cycle"] == 40
        assert "api_key" not in json.dumps(payload).lower()

    def test_api_logger_emits_info_under_uvicorn(self):
        import api

        api._configure_app_logging()
        package_logger = logging.getLogger("llm_drift_monitor")
        assert package_logger.level <= logging.INFO
        assert package_logger.handlers


class TestHealthDependencies:
    class Adapter:
        model_id = "gemini-3.1-flash-lite"

        def __init__(self, ok):
            self.ok = ok

        def ping(self):
            if not self.ok:
                raise RuntimeError("quota exhausted")
            return True

    class BrokenStorage:
        backend = "sqlite"
        location = "/broken.db"
        is_durable = False
        storage_error = None

        def count_successful_runs(self):
            raise RuntimeError("disk I/O error")

    @staticmethod
    def reset_provider_cache(monkeypatch):
        monkeypatch.setattr("api._provider_health",
                            {"at": 0.0, "ok": None, "status": None,
                             "error": None})

    def test_readiness_responses_are_not_cacheable(self):
        from fastapi.testclient import TestClient

        from api import app as api_app

        with TestClient(api_app, raise_server_exceptions=False) as client:
            for url in ("/health/live", "/health?probe=false"):
                response = client.get(url)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"

    def test_config_publishes_provider_health_ttl(self, tmp_path, monkeypatch):
        import api

        storage = DriftMonitorStorage(str(tmp_path / "ttl_config.db"))
        monitor = StubMonitor(storage, adapter=self.Adapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        api.invalidate_read_cache()
        payload = api.get_config()
        assert payload["provider_health_ttl_seconds"] == api.PROVIDER_HEALTH_TTL_SECONDS

    def test_database_and_provider_are_reported_independently(self, tmp_path, monkeypatch):
        from api import health

        storage = DriftMonitorStorage(str(tmp_path / "health.db"))
        monitor = StubMonitor(storage, adapter=self.Adapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        assert payload["status"] == "ok"
        assert payload["database"] == "available"
        assert payload["db"] == "ok"
        assert payload["provider_status"] == "ok"

        monitor.adapter = self.Adapter(False)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        assert payload["database"] == "available"
        assert payload["db"] == "ok"
        assert payload["provider_status"] == "unreachable"
        assert "quota exhausted" in payload["provider_error"]
        assert payload["status"] == "degraded"

    def test_database_failure_does_not_rewrite_provider_state(self, monkeypatch):
        from api import health

        monitor = StubMonitor(self.BrokenStorage(), adapter=self.Adapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        assert payload["database"] == "unavailable"
        assert payload["db"] == "unavailable"
        assert "disk I/O error" in payload["db_error"]
        assert payload["provider_status"] == "ok"
        assert payload["status"] == "degraded"

    def test_health_diagnostics_explain_timeout_and_keep_db_ready(self, tmp_path, monkeypatch):
        from api import health

        class TimeoutAdapter(self.Adapter):
            def ping(self):
                raise TimeoutError("ping timed out after 5s")

        storage = DriftMonitorStorage(str(tmp_path / "health_timeout.db"))
        monitor = StubMonitor(storage, adapter=TimeoutAdapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        d = payload["diagnostics"]
        assert d["db_ready"] is True
        assert d["provider_ready"] is False
        assert d["timeout"] is True
        # Request-level HTTP status is captured by the browser, never faked
        # by the handler that is producing this payload.
        assert "http_status" not in d
        assert d["error_type"] == "TimeoutError"
        assert "timed out" in d["error_detail"]
        assert d["duration_ms"] >= 0
        assert payload["database"] == "available"

    def test_health_diagnostics_redact_secrets(self, tmp_path, monkeypatch):
        from api import health

        class LeakyAdapter(self.Adapter):
            def ping(self):
                raise RuntimeError(
                    "request failed x-goog-api-key: super_secret_value_12345 "
                    "Authorization: Bearer another_secret_98765")

        storage = DriftMonitorStorage(str(tmp_path / "health_secret.db"))
        monitor = StubMonitor(storage, adapter=LeakyAdapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        blob = json.dumps(payload)
        assert "super_secret_value_12345" not in blob
        assert "another_secret_98765" not in blob
        assert "<redacted>" in blob

    def test_monitor_init_failure_keeps_db_available(self, monkeypatch, tmp_path):
        from api import health

        storage = DriftMonitorStorage(str(tmp_path / "health_no_monitor.db"))
        monkeypatch.setattr("api._get_storage", lambda: storage)

        def boom():
            raise RuntimeError("monitor exploded x-api-key: secret123")

        monkeypatch.setattr("api._get_monitor", boom)
        payload = health()
        assert payload["database"] == "available"
        assert payload["status"] == "degraded"
        assert payload["provider_status"] == "unreachable"
        assert payload["diagnostics"]["db_ready"] is True
        assert "secret123" not in json.dumps(payload)

    def test_storage_init_failure_still_returns_structured_health(self, monkeypatch):
        from api import health

        def boom():
            raise RuntimeError("storage init failed")

        monkeypatch.setattr("api._get_storage", boom)
        monkeypatch.setattr("api._get_monitor", boom)
        self.reset_provider_cache(monkeypatch)
        payload = health()
        assert payload["database"] == "unavailable"
        assert payload["storage_backend"] == "unknown"
        assert payload["diagnostics"]["db_ready"] is False
        assert "storage init failed" in payload["db_error"]

    def test_unchecked_provider_is_explicit(self, tmp_path, monkeypatch):
        from api import health

        storage = DriftMonitorStorage(str(tmp_path / "health_unchecked.db"))
        monitor = StubMonitor(storage, adapter=self.Adapter(True))
        monkeypatch.setattr("api._get_monitor", lambda: monitor)
        self.reset_provider_cache(monkeypatch)
        payload = health(probe=False)
        assert payload["provider_status"] == "unchecked"
        assert payload["diagnostics"]["provider_ready"] is False
        assert payload["diagnostics"]["error_detail"] is None


class TestStaticAssets:
    """Finding: FileResponse raises RuntimeError (HTTP 500) when the asset
    file is absent; a missing asset is a 404."""

    @staticmethod
    def client():
        from fastapi.testclient import TestClient

        from api import app as api_app
        return TestClient(api_app, raise_server_exceptions=False)

    def test_missing_asset_is_404(self):
        client = self.client()
        response = client.get("/static/does_not_exist.png")
        assert response.status_code == 404

    def test_present_asset_is_served(self):
        client = self.client()
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get(
            "content-type", "application/javascript")

    def test_path_traversal_is_contained(self):
        client = self.client()
        response = client.get("/static/..%2F..%2Fapi.py")
        assert response.status_code == 404

    def test_demo_artifact_is_served_from_the_committed_data_directory(self):
        client = self.client()
        response = client.get("/static/data/demo-scenarios.json")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
        assert response.json()["schema_version"] == 1

    def test_only_the_exact_demo_artifact_name_is_served(self):
        client = self.client()
        response = client.get("/static/data/..%2Findex.html")
        assert response.status_code == 404
        response = client.get("/static/data/unknown.json")
        assert response.status_code == 404
