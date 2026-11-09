"""Tests for the service layer: circuit breaker, matched windows, test dispatch."""

import sqlite3

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
        # A refusal rate change card computed from these fields is real data,
        # not a missing read.
        assert payload['refusal_rate_current'] - payload['refusal_rate_reference'] == pytest.approx(0.05)

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
