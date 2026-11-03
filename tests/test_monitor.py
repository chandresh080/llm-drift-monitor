"""Tests for monitor reference-window logic and alert persistence."""

import pytest
import yaml

from storage import DriftMonitorStorage
from monitor import LLMDriftMonitor


@pytest.fixture
def fake_monitor(tmp_path):
    """LLMDriftMonitor wired to a fake adapter and an isolated temp database."""
    with open("config.yaml", "r") as config_file:
        config = yaml.safe_load(config_file)
    config["target"] = {"type": "fake", "model_id": "fake-test", "temperature": 0.0}
    config["data"]["db_path"] = str(tmp_path / "test.db")
    config_path = str(tmp_path / "test-config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    return LLMDriftMonitor(config_path=config_path)


class TestWindowing:
    def test_reference_window_requires_minimum_runs(self, fake_monitor):
        """No reference should be available before the minimum run count."""
        assert fake_monitor.storage.count_successful_runs() == 0
        assert fake_monitor.create_reference_window() is False
        assert fake_monitor.has_sufficient_reference() is False

    def test_reference_window_created_once_sufficient_runs(self, fake_monitor):
        """Reference becomes sufficient once enough successful runs exist."""
        fake_monitor.run_canary_probes(num_prompts=10)
        assert fake_monitor.storage.count_successful_runs() == 10
        assert fake_monitor.has_sufficient_reference() is True
        assert fake_monitor.create_reference_window() is True


class TestAlertPersistence:
    def test_insert_and_retrieve_alert(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "alerts.db"))
        assert storage.get_all_alerts(limit=10) == []

        storage.insert_alert({
            'timestamp': '2026-11-02T00:00:00',
            'alert_type': 'drift',
            'severity': 'medium',
            'message': 'Behavioral drift detected.',
            'ks_pvalue': 0.001,
            'proportion_z_pvalue': 1.0,
            'mmd_pvalue': 0.002,
            'overall_drift_detected': 1,
            'overall_quality_degradation': 0,
        })

        alerts = storage.get_all_alerts(limit=10)
        assert len(alerts) == 1
        assert alerts[0]['alert_type'] == 'drift'
        assert alerts[0]['severity'] == 'medium'
        assert alerts[0]['overall_drift_detected'] == 1

    def test_alerts_respect_limit(self, tmp_path):
        storage = DriftMonitorStorage(str(tmp_path / "alerts.db"))
        for i in range(3):
            storage.insert_alert({
                'timestamp': f"2026-11-02T00:00:0{i}",
                'alert_type': 'drift',
                'severity': 'low',
                'message': f'Alert {i}',
                'overall_drift_detected': 0,
                'overall_quality_degradation': 0,
            })
        assert len(storage.get_all_alerts(limit=2)) == 2
        assert len(storage.get_all_alerts(limit=10)) == 3
