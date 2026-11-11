"""Generate the Gemini matched reference baseline for the hosted demo.

Runs the real monitoring pipeline against the real Gemini API:
config/config.gemini.yaml's model, decoding settings and the v3 panel,
four complete cycles of 40 probes each. The first three cycles build the
reference window (120 responses); the fourth is the current window, so the
stored drift tests in this baseline were actually computed by the same
code the service runs.

Rate discipline: free tier quotas apply, so cycles are spaced
CYCLE_SPACING_SECONDS apart and every stored cycle's provider latency and
token usage land in the database with the rest of the row. The adapter
retries provider 429 responses with exponential backoff within a cycle.

Output: data/seed_gemini.db (plus a baseline_manifest table recording
provider, model, panel version, decoding settings, embedding model and
collection time, so the reference window carries its own provenance).

Requires GEMINI_API_KEY in the environment or .env. Never prints it.
Usage:  python -m scripts.generate_reference_db
"""

import os
import tempfile
import time
from datetime import datetime, timezone

import yaml

from monitor import LLMDriftMonitor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEMINI_CONFIG = os.path.join(REPO_ROOT, "config", "config.gemini.yaml")
SEED_DB = os.path.join(REPO_ROOT, "data", "seed_gemini.db")

NUM_CYCLES = 4
CYCLE_SPACING_SECONDS = 100.0
# High demand waves (Gemini 503) clear after minutes, not seconds: the
# spacing between full cycle attempts is deliberately generous, and the
# resume path means a failed attempt never re-spends earlier cycles.
CYCLE_ATTEMPTS = 5
ATTEMPT_SPACING_SECONDS = 180.0



def build_monitor(db_path: str) -> LLMDriftMonitor:
    """The hosted Gemini config pointed at the baseline database path."""
    with open(GEMINI_CONFIG) as f:
        config = yaml.safe_load(f)
    config["data"]["db_path"] = db_path
    config_path = os.path.join(tempfile.gettempdir(), "reference-config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    return LLMDriftMonitor(config_path=config_path)


def write_manifest(monitor: LLMDriftMonitor) -> None:
    """Record how this database's reference window was produced.

    On resume an existing manifest is left alone: collected_at documents
    when the collection started, it is not a stamp of the last rerun.
    """
    existing = monitor.storage.get_manifest()
    if existing.get("model_id"):
        return
    cfg = monitor.config
    target = cfg["target"]
    monitor.storage.insert_manifest({
        "provider": "gemini",
        "model_id": target["model_id"],
        "panel_version": monitor.canary_prompts["version"],
        "panel_prompts": str(len(monitor.canary_prompts["prompts"])),
        "temperature": str(target.get("temperature", 0.0)),
        "max_tokens": str(target.get("max_tokens", 512)),
        "embedding_model": cfg["embeddings"]["model_name"],
        "reference_window": str(cfg["reference"]["window_size"]),
        "config_file": "config/config.gemini.yaml",
        "generator_script": "scripts/generate_reference_db.py",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    })


def main() -> None:
    os.makedirs(os.path.dirname(SEED_DB), exist_ok=True)
    resume = os.path.exists(SEED_DB)
    monitor = build_monitor(SEED_DB)
    model_id = monitor.adapter.model_id
    probes_per_cycle = monitor.config["monitoring"]["batch_size"]
    # Attribution by all rows: failed status rows (e.g. empty responses
    # to recitation guarded probes) are still exactly one row per probe
    # of their cycle, so cycle counting must not exclude them.
    stored_runs = monitor.storage.count_all_runs() if resume else 0
    if not resume:
        # A fresh collection deletes the file so a failed partial cycle can
        # never masquerade as the baseline.
        os.remove(SEED_DB)
        stored_runs = 0
    first_cycle = stored_runs // probes_per_cycle
    if first_cycle * probes_per_cycle != stored_runs:
        # Incomplete trailing data cannot be attributed to a cycle: refuse
        # to build on it rather than silently mixing.
        raise SystemExit(
            f"{SEED_DB} holds {stored_runs} runs, not a multiple of the "
            f"{probes_per_cycle} per cycle; delete it and rerun from scratch.")
    if resume:
        print(f"Resuming: {stored_runs} runs already stored "
              f"({first_cycle} complete cycles).")
    print(f"Collecting the Gemini baseline with {model_id}: "
          f"{NUM_CYCLES} cycles x {probes_per_cycle} prompts, "
          f"{CYCLE_SPACING_SECONDS:.0f}s between cycles.")

    write_manifest(monitor)

    for i in range(first_cycle, NUM_CYCLES):
        if i > first_cycle:
            print(f"waiting {CYCLE_SPACING_SECONDS:.0f}s before the next "
                  "cycle (free tier rate spacing)...", flush=True)
            time.sleep(CYCLE_SPACING_SECONDS)
        run_cycle_with_retry(monitor, i + 1)

    validate(monitor)


def run_cycle_with_retry(monitor: LLMDriftMonitor, cycle_number: int) -> None:
    """One cycle, retried across transient provider storms.

    A failed attempt stores no rows (probe rows are inserted only after the
    whole batch answers), so a retry starts that cycle cleanly. High demand
    waves (Gemini 503) and quota blips clear after a wait; retrying here is
    what makes a 40 prompt collection survive a multi minute outage.
    """
    for attempt in range(1, CYCLE_ATTEMPTS + 1):
        try:
            started = time.perf_counter()
            result = monitor.run_monitoring_cycle()
            payload = result.get("results", {})
            elapsed = time.perf_counter() - started
            tokens = payload.get("probe_tokens_total")
            print(f"cycle {cycle_number}/{NUM_CYCLES} "
                  f"(attempt {attempt}): tests_run={result.get('tests_run')} "
                  f"reason={result.get('reason', '-')} wall={elapsed:.1f}s "
                  f"latency_mean={payload.get('provider_latency_mean_seconds')}s "
                  f"tokens={tokens if tokens is not None else 'n/a'}",
                  flush=True)
            for alert in result.get("alerts", []):
                print(f"        alert: {alert}", flush=True)
            return
        except RuntimeError as exc:
            print(f"cycle {cycle_number} attempt {attempt} failed: "
                  f"{str(exc)[:400]}", flush=True)
            if attempt == CYCLE_ATTEMPTS:
                raise
            print(f"waiting {ATTEMPT_SPACING_SECONDS:.0f}s before the next "
                  "attempt...", flush=True)
            time.sleep(ATTEMPT_SPACING_SECONDS)


def validate(monitor: LLMDriftMonitor) -> None:
    """Refuse to call the baseline usable unless it genuinely is one."""
    print("\nValidation:")
    manifest = monitor.storage.get_manifest()
    ok = True
    if manifest.get("provider") == "gemini" and manifest.get("model_id"):
        print(f"  manifest: provider={manifest['provider']} "
              f"model={manifest['model_id']} "
              f"panel={manifest.get('panel_version')} "
              f"collected={manifest.get('collected_at')}")
    else:
        print("  manifest: MISSING provider/model fields")
        ok = False
    # Count every stored row, matching the resume logic: recitation guarded
    # probes legitimately store a failed status row with an empty response,
    # and excluding them would undercount a complete collection.
    runs = monitor.storage.count_all_runs()
    expected = (monitor.config["reference"]["window_size"]
                + monitor.config["monitoring"]["batch_size"])
    print(f"  stored runs: {runs} (expected >= {expected})")
    ok = ok and runs >= expected
    results = monitor.storage.get_all_test_results(limit=5)
    if not results:
        print("  test results: NONE stored, the baseline cannot serve drift")
        ok = False
    for r in results:
        paired_pairs = r.get("paired_cluster_prompt_pairs")
        unit = r.get("mmd_permutation_unit")
        pvals = [r.get("ks_pvalue"), r.get("proportion_z_pvalue"),
                 r.get("mmd_pvalue")]
        sane = all(p is None or 0.0 <= p <= 1.0 for p in pvals)
        print(f"  {r['timestamp']}: dist_shift derived, "
              f"ks_p={r.get('ks_pvalue')} mmd_p={r.get('mmd_pvalue')} "
              f"({unit}, {paired_pairs} prompt pairs) "
              f"z_p={r.get('proportion_z_pvalue')} "
              f"latency={r.get('provider_latency_mean_seconds')}s sane_p={sane}")
        if not sane or paired_pairs is None or unit != "prompt_cluster":
            print("  ^ does not look like a completed paired inference run")
            ok = False
    if not ok:
        raise SystemExit("Baseline validation failed; do not ship this DB.")
    print(f"\nBaseline written to {SEED_DB} and validated.")


if __name__ == "__main__":
    main()
