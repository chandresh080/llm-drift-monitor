"""Tests for the static Demo artifact.

The committed artifact is validated as a frontend data contract. The generator
tests exercise the full monitoring pipeline with a tiny deterministic embedder
so they stay offline and fast; they do not replace the committed artifact,
which is produced once with the configured local embedding model.
"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import monitor as monitor_module
from scripts import generate_demo_artifacts as demo

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = REPO_ROOT / "web" / "data" / "demo-scenarios.json"


class StubEmbedder:
    """Deterministic stand-in for the local embedding model."""

    def extract_embeddings(self, texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256((text or "dummy").encode("utf-8")).digest()
            vectors.append([value / 255.0 for value in digest[:8]])
        return vectors

    def extract_response_features(self, text):
        return len(text or ""), bool(text and text.strip())

    def release(self):
        return None


def stub_factory():
    return StubEmbedder()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestCommittedArtifact:
    @staticmethod
    def load():
        return json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))

    def test_schema_and_configuration_match_the_deployed_pipeline(self):
        artifact = self.load()
        assert artifact["schema_version"] == 1
        assert artifact["source"] == "scripts.generate_demo_artifacts"
        method = artifact["methodology"]
        assert "synthetic" in method["disclosure"].lower()
        assert "no gemini" in method["disclosure"].lower()
        assert "run_monitoring_cycle()" in method["pipeline"]
        assert "Evaluation view" in method["limitation"]

        config = artifact["configuration"]
        assert config["config_source"] == "config/config.gemini.yaml"
        assert config["adapter"] == "deterministic offline synthetic adapter"
        assert config["storage_backend"] == "sqlite"
        assert config["prompt_panel_version"] == "3.0.0"
        assert config["prompts_per_cycle"] == 40
        assert config["reference_window"] == 120
        assert config["alpha"] == 0.05
        assert config["min_consecutive_cycles"] == 2
        assert config["correction"] == "Benjamini-Hochberg"
        assert config["detector_family"] == [
            "Response length", "Response availability", "Semantic shift"]

    def test_each_scenario_has_six_complete_deterministic_cycles(self):
        artifact = self.load()
        assert [s["id"] for s in artifact["scenarios"]] == [
            "stable", "injected-drift"]

        for scenario in artifact["scenarios"]:
            assert scenario["reference_cycles"] == 3
            assert scenario["tests_start_cycle"] == 4
            assert len(scenario["cycles"]) == 6
            assert [c["cycle_number"] for c in scenario["cycles"]] == list(range(1, 7))
            assert [c["phase"] for c in scenario["cycles"]] == [
                "reference", "reference", "reference",
                "comparison", "comparison", "comparison"]
            for cycle in scenario["cycles"]:
                assert cycle["probe_count"] == 40
                assert cycle["tests_run"] is (cycle["cycle_number"] >= 4)
                if not cycle["tests_run"]:
                    # The comparison window is not ready during baseline
                    # collection, so there is no before/after pair to show.
                    assert cycle["samples"] == []
                    assert "results" not in cycle
                    assert cycle["alerts"] == []
                    continue
                assert len(cycle["samples"]) == 5
                detectors = cycle["results"]["detectors"]
                assert set(detectors) == {"length", "availability", "semantic"}
                for row in detectors.values():
                    assert 0.0 <= row["raw_p"] <= 1.0
                    assert 0.0 <= row["adjusted_p"] <= 1.0
                assert cycle["results"]["reference_size"] == 120
                assert cycle["results"]["current_size"] == 40

    def test_stable_scenario_never_flags_and_drift_obeys_the_alert_policy(self):
        artifact = self.load()
        stable, drift = artifact["scenarios"]

        assert stable["summary"]["verdict"] == "No shift"
        assert stable["summary"]["flagged_cycles"] == 0
        assert stable["summary"]["alerts"] == 0
        for cycle in stable["cycles"]:
            assert cycle["adapter_mode"] == "stable"
        for cycle in stable["cycles"][3:]:
            detectors = cycle["results"]["detectors"]
            assert not any(row["significant"] for row in detectors.values())
        for sample in stable["cycles"][-1]["samples"]:
            assert sample["changed"] is False

        assert [c["adapter_mode"] for c in drift["cycles"]] == [
            "stable", "stable", "stable", "shifted", "shifted", "shifted"]
        assert drift["summary"]["verdict"] == "Alert fired"
        assert drift["summary"]["flagged_cycles"] == 3
        assert drift["summary"]["alerts"] == 2
        first, second, third = drift["cycles"][3:]
        assert all(c["results"]["distribution_shift_detected"] for c in (first, second, third))
        assert first["alerts"] == []
        assert len(second["alerts"]) == 1
        assert len(third["alerts"]) == 1
        assert second["alerts"][0]["type"] == "drift"
        for cycle in (first, second, third):
            detectors = cycle["results"]["detectors"]
            assert detectors["length"]["significant"] is True
            assert detectors["semantic"]["significant"] is True
            assert detectors["availability"]["significant"] is False
            assert cycle["results"]["effects"]["length_shift_pct"] > 0
            assert cycle["results"]["effects"]["centroid_distance"] > 0
        assert [c["mean_response_length"] for c in drift["cycles"]][:3] == [
            79.0, 79.0, 79.0]
        assert all(c["mean_response_length"] > 300 for c in drift["cycles"][3:])
        assert all(s["changed"] for s in drift["cycles"][-1]["samples"])


class TestArtifactGeneration:
    def test_generation_is_isolated_from_live_storage_and_keys(self, tmp_path, monkeypatch):
        sentinel_db = tmp_path / "sentinel.db"
        sentinel_db.write_text("do not touch", encoding="utf-8")
        database_url = "postgresql://sentinel@example.invalid/db"
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("GEMINI_API_KEY", "sentinel-key")
        called = []
        monkeypatch.setattr(
            monitor_module, "load_provider_key_secret_files",
            lambda *args, **kwargs: called.append(args))

        seed_files = sorted((REPO_ROOT / "data").glob("*.db"))
        before = {path.name: file_digest(path) for path in seed_files}
        output = tmp_path / "artifact.json"

        artifact = demo.generate_artifact(
            output_path=str(output), embedder_factory=stub_factory)

        assert output.is_file()
        assert artifact["configuration"]["storage_backend"] == "sqlite"
        assert os.environ["DATABASE_URL"] == database_url
        assert os.environ["GEMINI_API_KEY"] == "sentinel-key"
        assert called == []
        after = {path.name: file_digest(path)
                 for path in sorted((REPO_ROOT / "data").glob("*.db"))}
        assert after == before
        assert set(after) == set(before)

    def test_generation_is_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.setattr(demo, "CYCLES_PER_SCENARIO", 4)
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        demo.generate_artifact(output_path=str(first), embedder_factory=stub_factory)
        demo.generate_artifact(output_path=str(second), embedder_factory=stub_factory)
        assert first.read_bytes() == second.read_bytes()

    @pytest.mark.parametrize("backend,path", [
        ("postgres", "/tmp/not-sqlite.db"),
        ("sqlite", "/etc/passwd"),
    ])
    def test_generator_refuses_storage_outside_its_temp_root(
            self, tmp_path, monkeypatch, backend, path):
        class FakeMonitor:
            def __init__(self, config_path):
                self.storage = SimpleNamespace(
                    backend=backend, accepts_writes=True, resolved_path=path)

        monkeypatch.setattr(demo, "LLMDriftMonitor", FakeMonitor)
        with pytest.raises(RuntimeError, match="Demo generation refused"):
            demo.generate_artifact(
                output_path=str(tmp_path / "artifact.json"),
                embedder_factory=stub_factory)
