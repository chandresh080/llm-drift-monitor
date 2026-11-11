"""The recorded provider comparison artifact stays regenerable and honest.

The dashboard's evaluation tab loads web/provider_comparison.json, which is
committed. This test re-derives it from the committed experiment outputs
into a temporary path and asserts the fields the frontend renders, so the
artifact can never drift from the sources or lose its caveat without a
failing test. No network calls: everything is read from the repo files.
"""

import importlib.util
import json
import os
import tempfile

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "export_provider_comparison.py")


@pytest.fixture(scope="module")
def exporter():
    spec = importlib.util.spec_from_file_location(
        "export_provider_comparison", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifact_rederives_from_committed_sources(exporter):
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "provider_comparison.json")
        exporter.OUT = out
        exporter.main()
        payload = json.load(open(out))

    assert payload["generated_by"] == "scripts/export_provider_comparison.py"
    # The recorded experiment: 5 Groq reference cycles, 6 Gemini current
    # cycles (1 comparison + 5 extension), every cycle complete.
    assert len(payload["reference"]["cycles"]) == 5
    assert len(payload["current"]["cycles"]) == 6
    for cycle in payload["reference"]["cycles"] + payload["current"]["cycles"]:
        assert cycle["failed_runs"] == 0
        assert cycle["ok_runs"] == 10
        assert cycle["latency_p50_s"] > 0
        assert cycle["tokens_total"] > 0
    # Provider identities come from the recorded setup, not from hand entry.
    assert payload["reference"]["model"] == "openai/gpt-oss-20b"
    assert payload["current"]["model"] == "gemini-3.1-flash-lite"
    # No detector family flagged in either stage.
    for stage in ("single_cycle", "extended_final"):
        families = payload["families"][stage]
        assert families["any_significant"] is False
        assert families["paired_prompt_pairs"] == 10
    # The caveat that keeps this from being mistaken for a drift result.
    assert "Cross-provider" in payload["caveat"]
    assert "not be attributed exclusively to provider behavior" in payload["caveat"]
    # Uncontrolled differences are carried verbatim from the experiment.
    assert payload["shared"]["uncontrolled_differences"]


def test_no_provider_key_material_in_artifact(exporter):
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "provider_comparison.json")
        exporter.OUT = out
        exporter.main()
        raw = open(out).read()
    for marker in ("gsk_", "grok_", "AIza", "api_key"):
        assert marker not in raw
