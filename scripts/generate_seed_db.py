"""Generate a seed SQLite database for the public demo deployment.

Builds a realistic monitoring history using only real code paths and the
deterministic FakeLLMAdapter (no network, no keys):

  Era 1 ("null"):    adapter seed 42  -> 50 probes / 5 cycles  (baseline,
                     tests can't fire yet: need ref_size + cur_size = 60 runs)
  Era 2 ("shift"):   adapter seed 999 -> 50 probes / 5 cycles; from the 60th
                     run onward every cycle runs the drift tests against the
                     seed-42 reference window, storing test_results + alerts.

Output: data/seed_drift.db
Usage:  python -m scripts.generate_seed_db
"""

import os
import tempfile
import yaml

from monitor import LLMDriftMonitor, FakeLLMAdapter

NULL_SEED = 42
SHIFT_SEED = 999
PROBES_PER_CYCLE = 10

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLOUD_CONFIG = os.path.join(REPO_ROOT, "config", "config.cloud.yaml")
SEED_DB = os.path.join(REPO_ROOT, "data", "seed_drift.db")


def build_monitor(db_path: str) -> LLMDriftMonitor:
    """Cloud config pointed at the seed database path."""
    with open(CLOUD_CONFIG) as f:
        config = yaml.safe_load(f)
    config["data"]["db_path"] = db_path
    config["target"] = {"type": "fake", "model_id": "fake-llm-v1",
                        "temperature": 0.0, "seed": NULL_SEED}
    config_path = os.path.join(tempfile.gettempdir(), "seed-config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    return LLMDriftMonitor(config_path=config_path)


def run_era(monitor: LLMDriftMonitor, seed: int, num_cycles: int, label: str) -> None:
    new_adapter = FakeLLMAdapter(model_id="fake-llm-v1", seed=seed)
    monitor.adapter = new_adapter  # inject next era / injected shift
    monitor.adapter.model_id = "fake-llm-v1"
    for i in range(1, num_cycles + 1):
        result = monitor.run_monitoring_cycle(num_probes=PROBES_PER_CYCLE)
        print(f"[{label}] cycle {i}: tests_run={result.get('tests_run')} "
              f"drift={result.get('overall_drift_detected', '-')} "
              f"flagged={result.get('bh_significant_tests', '-')} "
              f"quality={result.get('quality_degradation_detected', '-')}")
        for alert in result.get("alerts", []):
            print(f"        alert: {alert}")


def main() -> None:
    if os.path.exists(SEED_DB):
        os.remove(SEED_DB)
    os.makedirs(os.path.dirname(SEED_DB), exist_ok=True)
    monitor = build_monitor(SEED_DB)

    print("Era 1: null baseline (seed 42)")
    run_era(monitor, NULL_SEED, num_cycles=5, label="null")

    print("\nEra 2: injected behavioral shift (seed 999)")
    run_era(monitor, SHIFT_SEED, num_cycles=5, label="shift")

    n_runs = monitor.storage.count_successful_runs()
    test_results = monitor.storage.get_all_test_results(limit=20)
    alerts = monitor.storage.get_all_alerts(limit=20)
    print(f"\nseed DB: {SEED_DB}")
    print(f"successful runs: {n_runs}")
    print(f"test results stored: {len(test_results)}")
    for r in test_results:
        print(f"  drift={r.get('overall_drift_detected')} "
              f"ks_p={r.get('ks_pvalue')} z_p={r.get('proportion_z_pvalue')} "
              f"mmd_p={r.get('mmd_pvalue')} flagged={r.get('bh_significant_tests')}")
    print(f"alerts stored: {len(alerts)}")
    for a in alerts:
        print(f"  [{a.get('severity')}] {a.get('alert_type')}: {a.get('message')[:80]}")


if __name__ == "__main__":
    main()
