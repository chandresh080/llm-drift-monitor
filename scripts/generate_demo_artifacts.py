"""Generate the static Demo dashboard artifact.

The artifact is built from the project's real monitoring pipeline, not from
precomputed detector verdicts:

  * a deterministic, offline response adapter supplies synthetic answers;
  * LLMDriftMonitor.run_monitoring_cycle() stores them in a temporary SQLite
    database and runs the configured detectors, BH correction and alert
    policy;
  * the resulting cycles are reduced to a compact, read-only JSON file for
    the Demo tab.

Safety properties, enforced in code rather than left to convention:

  * DATABASE_URL is explicitly neutralised before the monitor is constructed,
    and the generator refuses to continue unless storage is the temporary
    SQLite file it created;
  * the deployed statistical configuration is reused, but the target adapter
    is replaced with the offline synthetic adapter, so no provider key or
    network call is involved;
  * model weights are resolved from the local HF cache only;
  * no committed database is opened, deleted or overwritten.

Usage:  python -m scripts.generate_demo_artifacts

The output is web/data/demo-scenarios.json. Regenerate it deliberately; the
dashboard never runs a monitoring cycle to populate the Demo view.
"""

import contextlib
import copy
import hashlib
import json
import math
import os
import tempfile
from typing import Any, Callable, Dict, Iterator, List, Optional

import yaml

import monitor as monitor_module
from monitor import FakeLLMAdapter, LLMDriftMonitor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "config.gemini.yaml")
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "web", "data", "demo-scenarios.json")

ARTIFACT_VERSION = 1
MODEL_ID = "demo-synthetic-v1"
CYCLES_PER_SCENARIO = 6
PROVIDER_KEY_NAMES = ("GEMINI_API_KEY", "GROQ_API_KEY",
                      "OPENAI_API_KEY", "XAI_API_KEY")
SAMPLE_PROMPT_IDS = (
    "fact_001", "reas_009", "code_017", "refu_026", "form_033",
)

SCENARIOS = (
    {
        "id": "stable",
        "label": "Stable behavior",
        "summary": "The same deterministic responses across all six cycles.",
        "description": "Reference collection runs first; the three evaluated cycles compare "
                       "an unchanged response distribution with the same fixed baseline.",
        "shift_start_cycle": None,
    },
    {
        "id": "injected-drift",
        "label": "Injected behavioral drift",
        "summary": "A fixed, deliberately different response style from cycle 4.",
        "description": "The first three cycles build the baseline. From cycle 4, the adapter "
                       "switches to a longer, different answer style. The first flagged cycle "
                       "stays silent; the alert policy fires once the flag repeats.",
        "shift_start_cycle": 4,
    },
)

METHODOLOGY = {
    "disclosure": (
        "Every response in this view is synthetic. No Gemini, Groq, OpenAI, Ollama, "
        "provider key, live monitoring database or live API call was used to produce "
        "the artifact. The responses are deterministic for a fixed seed and prompt."
    ),
    "pipeline": (
        "The synthetic adapter feeds the same LLMDriftMonitor.run_monitoring_cycle() path "
        "used by a real check: response features and local embeddings are stored in a "
        "temporary SQLite database, then the configured detectors, Benjamini-Hochberg "
        "correction and consecutive-cycle alert policy decide each cycle."
    ),
    "limitation": (
        "These charts are a controlled demonstration, not evidence about any production "
        "model. The measured statistical benchmark lives in the separate Evaluation view."
    ),
    "artifact_source": "web/data/demo-scenarios.json",
}


@contextlib.contextmanager
def offline_environment() -> Iterator[None]:
    """Make storage and model resolution offline for the whole generation."""
    overrides = {
        # load_dotenv() does not override an existing variable. An empty value
        # therefore neutralises both the process environment and a local .env.
        "DATABASE_URL": "",
        "DRIFT_MONITOR_TRACKING": "0",
        "DRIFT_MONITOR_EMBEDDING_BACKEND": "onnx",
        "HF_HUB_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    # The fake target needs no credentials. Clearing these names and skipping
    # the secret-file loader guarantees that generation cannot pick up a
    # provider key from Render's /etc/secrets or a developer's shell.
    overrides.update({name: "" for name in PROVIDER_KEY_NAMES})
    previous = {name: os.environ.get(name) for name in overrides}
    for name, value in overrides.items():
        os.environ[name] = value
    load_keys = monitor_module.load_provider_key_secret_files
    monitor_module.load_provider_key_secret_files = lambda *args, **kwargs: None
    try:
        yield
    finally:
        monitor_module.load_provider_key_secret_files = load_keys
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def working_directory(path: str) -> Iterator[None]:
    """Resolve repository-relative config and prompt files predictably."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class SyntheticDemoAdapter:
    """Offline adapter with a stable mode and one deterministic shifted mode.

    The stable mode reuses FakeLLMAdapter, so the baseline is produced by the
    project's existing synthetic generator. The shifted mode is a different,
    fixed answer style selected by hashes of the seed and prompt alone. The
    shift changes response length and semantics without changing validity, so
    the quality family remains separate from the distributional verdict.
    """

    model_id = MODEL_ID

    def __init__(self, seed: int = 42):
        self.seed = seed
        self.base = FakeLLMAdapter(model_id=self.model_id, seed=seed)
        self.shifted = False

    def generate(self, prompt: str) -> str:
        if not self.shifted:
            return self.base.generate(prompt)
        choice = int(
            hashlib.sha256(f"{self.seed}:shift:{prompt}".encode()).hexdigest(), 16
        ) % len(_SHIFTED_STYLES)
        return _SHIFTED_STYLES[choice] * 2 + f"Context: {prompt[:60]}."

    def generate_batch(self, prompts: List[str]) -> List[str]:
        return [self.generate(prompt) for prompt in prompts]

    def ping(self) -> bool:
        return True


_SHIFTED_STYLES = (
    "From a reliability standpoint, the answer requires careful validation. "
    "Operational details matter because edge cases and failure modes change "
    "the conclusion. ",
    "I would approach this by separating assumptions from evidence, then "
    "checking the most likely failure modes before recommending a course of "
    "action. ",
    "A concise answer is insufficient here; the important part is the "
    "tradeoff between accuracy, maintainability, and the operating context. ",
)


def _number(value: Any) -> Optional[float]:
    """JSON-safe finite number."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _flag(value: Any) -> bool:
    return bool(value)


def _load_config(db_path: str) -> Dict[str, Any]:
    """Reuse deployed statistical settings with an offline target."""
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config = copy.deepcopy(config)
    config["target"] = {
        "type": "fake",
        "model_id": MODEL_ID,
        "temperature": 0.0,
        "max_workers": 2,
        "call_spacing_seconds": 0.0,
    }
    config["data"]["db_path"] = db_path
    return config


def _build_monitor(config_path: str,
                   embedder_factory: Optional[Callable[[], Any]] = None
                   ) -> LLMDriftMonitor:
    monitor = LLMDriftMonitor(config_path=config_path)
    if embedder_factory is not None:
        # Tests inject a tiny deterministic embedder so the full pipeline can
        # be exercised without loading model weights. Production generation
        # always uses the configured local embedding model.
        monitor.embedder = embedder_factory()
    return monitor


def _detector_rows(results: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        "length": {
            "name": "Response length",
            "method": "KS",
            "raw_p": _number(results.get("ks_pvalue")),
            "adjusted_p": _number(results.get("ks_adjusted_pvalue")),
            "significant": _flag(results.get("ks_significant")),
        },
        "availability": {
            "name": "Response availability",
            "method": results.get("proportion_test_method") or "paired_permutation",
            "raw_p": _number(results.get("proportion_z_pvalue")),
            "adjusted_p": _number(results.get("proportion_z_adjusted_pvalue")),
            "significant": _flag(results.get("proportion_z_significant")),
        },
        "semantic": {
            "name": "Semantic shift",
            "method": "Paired MMD",
            "raw_p": _number(results.get("mmd_pvalue")),
            "adjusted_p": _number(results.get("mmd_adjusted_pvalue")),
            "significant": _flag(results.get("mmd_significant")),
        },
    }


def _category_rows(results: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for name, test in sorted((results.get("category_tests") or {}).items()):
        rows.append({
            "name": name,
            "p": _number(test.get("pvalue")),
            "adjusted_p": _number(test.get("adjusted_pvalue")),
            "significant": _flag(test.get("significant")),
            "decision": test.get("decision"),
            "n_reference": test.get("n_reference"),
            "n_current": test.get("n_current"),
        })
    return rows


def _samples(comparison: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [row for row in (comparison.get("rows") or [])
            if row.get("prompt_id") in SAMPLE_PROMPT_IDS]
    by_id = {row.get("prompt_id"): row for row in rows}

    def describe(value: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "text": value.get("text"),
            "length": _number(value.get("length")),
            "is_valid": value.get("is_valid"),
        }

    result = []
    for prompt_id in SAMPLE_PROMPT_IDS:
        row = by_id.get(prompt_id)
        if not row:
            continue
        reference = describe(row.get("reference") or {})
        current = describe(row.get("current") or {})
        result.append({
            "prompt_id": prompt_id,
            "label": row.get("label") or prompt_id,
            "category": row.get("category"),
            "redacted": bool(row.get("prompt_redacted")),
            "reference": reference,
            "current": current,
            "changed": reference["text"] != current["text"],
        })
    return result


def _cycle_mean_length(monitor: LLMDriftMonitor, cycle_id: str) -> Optional[float]:
    """Mean stored response length for this cycle, independent of window readiness."""
    values = [_number(row.get("response_length"))
              for row in monitor.storage.get_all_runs_ordered()
              if row.get("cycle_id") == cycle_id
              and row.get("run_status") == "success"]
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 1) if values else None


def _cycle_payload(monitor: LLMDriftMonitor, cycle_number: int,
                   cycle: Dict[str, Any], adapter_mode: str,
                   phase: str) -> Dict[str, Any]:
    results = cycle.get("results") or {}
    comparison = monitor.get_window_comparison()
    tested = bool(cycle.get("tests_run"))
    payload: Dict[str, Any] = {
        "cycle_number": cycle_number,
        "phase": phase,
        "adapter_mode": adapter_mode,
        "tests_run": tested,
        "reason": cycle.get("reason"),
        "probe_count": len(cycle.get("run_ids") or []),
        "mean_response_length": _cycle_mean_length(
            monitor, str(cycle.get("cycle_id") or "")),
        "samples": _samples(comparison),
        "alerts": [],
    }
    if tested:
        payload["results"] = {
            "reference_size": results.get("reference_size"),
            "current_size": results.get("current_size"),
            "detectors": _detector_rows(results),
            "bh_significant_tests": results.get("bh_significant_tests") or [],
            "distribution_shift_detected": _flag(
                results.get("distribution_shift_detected")),
            "quality_change_detected": _flag(results.get("quality_change_detected")),
            "quality_guardrail": _flag(results.get("overall_quality_degradation")),
            "effects": {
                "length_shift_pct": _number(results.get("length_shift_pct")),
                "centroid_distance": _number(results.get("centroid_distance")),
                "refusal_rate_reference": _number(results.get("ref_refusal_rate")),
                "refusal_rate_current": _number(results.get("cur_refusal_rate")),
                "refusal_rate_change": _number(results.get("refusal_rate_change")),
                "paired_mmd_statistic": _number(
                    results.get("paired_cluster_mmd_statistic")),
                "paired_mmd_pvalue": _number(
                    results.get("paired_cluster_mmd_pvalue")),
                "matched_prompts": results.get("paired_cluster_prompt_pairs"),
            },
            "categories": _category_rows(results),
        }
        for alert in cycle.get("alerts") or []:
            payload["alerts"].append({
                "type": alert.get("alert_type"),
                "severity": alert.get("severity"),
                "message": alert.get("message"),
            })
    return payload


def _scenario_summary(cycles: List[Dict[str, Any]]) -> Dict[str, Any]:
    evaluated = [cycle for cycle in cycles if cycle.get("tests_run")]
    flagged = [cycle for cycle in evaluated
               if (cycle.get("results") or {}).get("distribution_shift_detected")]
    alerts = sum(len(cycle.get("alerts") or []) for cycle in cycles)
    return {
        "evaluated_cycles": len(evaluated),
        "flagged_cycles": len(flagged),
        "alerts": alerts,
        "verdict": ("Alert fired" if alerts else
                    "Flag without alert" if flagged else "No shift"),
    }


def _run_scenario(temp_root: str, scenario: Dict[str, Any],
                  embedder_factory: Optional[Callable[[], Any]]) -> Dict[str, Any]:
    db_path = os.path.join(temp_root, f"{scenario['id']}.sqlite")
    config_path = os.path.join(temp_root, f"{scenario['id']}.yaml")
    with open(config_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(_load_config(db_path), handle, sort_keys=True)

    monitor = _build_monitor(config_path, embedder_factory)
    if monitor.storage.backend != "sqlite":
        raise RuntimeError("Demo generation refused: storage is not SQLite")
    if not monitor.storage.accepts_writes:
        raise RuntimeError("Demo generation refused: storage is read-only")
    resolved = os.path.realpath(monitor.storage.resolved_path)
    if os.path.commonpath([resolved, os.path.realpath(temp_root)]) != os.path.realpath(temp_root):
        raise RuntimeError(f"Demo generation refused: database outside temp root ({resolved})")

    adapter = SyntheticDemoAdapter(seed=int(monitor.random_seed))
    monitor.adapter = adapter
    reference_cycles = int(
        monitor.config["reference"]["window_size"]
        / monitor.config["monitoring"]["batch_size"])
    shift_start = scenario.get("shift_start_cycle")
    if shift_start is None:
        shift_start = CYCLES_PER_SCENARIO + 1
    cycles = []
    try:
        for cycle_number in range(1, CYCLES_PER_SCENARIO + 1):
            adapter.shifted = cycle_number >= shift_start
            phase = "reference" if cycle_number <= reference_cycles else "comparison"
            raw = monitor.run_monitoring_cycle()
            cycles.append(_cycle_payload(monitor, cycle_number, raw,
                                         "shifted" if adapter.shifted else "stable",
                                         phase))
    finally:
        monitor.storage.close()

    payload = dict(scenario)
    payload["reference_cycles"] = reference_cycles
    payload["tests_start_cycle"] = reference_cycles + 1
    payload["cycles"] = cycles
    payload["summary"] = _scenario_summary(cycles)
    return payload


def generate_artifact(output_path: Optional[str] = None,
                      embedder_factory: Optional[Callable[[], Any]] = None
                      ) -> Dict[str, Any]:
    """Generate, validate and write the compact Demo artifact."""
    output = os.path.abspath(output_path or DEFAULT_OUTPUT)
    with offline_environment(), tempfile.TemporaryDirectory(
            prefix="llm-drift-demo-") as temp_root, working_directory(REPO_ROOT):
        config = _load_config(os.path.join(temp_root, "unused.sqlite"))
        artifact = {
            "schema_version": ARTIFACT_VERSION,
            "source": "scripts.generate_demo_artifacts",
            "methodology": dict(METHODOLOGY),
            "configuration": {
                "config_source": "config/config.gemini.yaml",
                "adapter": "deterministic offline synthetic adapter",
                "storage_backend": "sqlite",
                "prompt_panel_version": None,
                "prompts_per_cycle": config["monitoring"]["batch_size"],
                "reference_window": config["reference"]["window_size"],
                "alpha": _number(config["alerts"]["significance_threshold"]),
                "min_consecutive_cycles": config["alerts"]["min_consecutive_cycles"],
                "correction": "Benjamini-Hochberg",
                "detector_family": ["Response length", "Response availability",
                                    "Semantic shift"],
                "embedding_model": config["embeddings"]["model_name"],
            },
            "scenarios": [
                _run_scenario(temp_root, scenario, embedder_factory)
                for scenario in SCENARIOS
            ],
        }
        panel_version = None
        with open(os.path.join("config", "canary_prompts.json"), encoding="utf-8") as handle:
            panel_version = json.load(handle).get("version")
        artifact["configuration"]["prompt_panel_version"] = panel_version

    os.makedirs(os.path.dirname(output), exist_ok=True)
    temporary_output = output + ".tmp"
    with open(temporary_output, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    os.replace(temporary_output, output)
    return artifact


def main() -> None:
    artifact = generate_artifact()
    print(f"wrote {DEFAULT_OUTPUT}")
    for scenario in artifact["scenarios"]:
        summary = scenario["summary"]
        print(f"  {scenario['id']}: {summary['evaluated_cycles']} evaluated cycles, "
              f"{summary['flagged_cycles']} flagged, {summary['alerts']} alerts "
              f"({summary['verdict']})")


if __name__ == "__main__":
    main()
