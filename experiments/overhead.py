"""Monitoring overhead: what does a cycle cost besides model inference?

One of the five headline metrics is p95 latency. The runtime logs per call
model latency each cycle (latency_p50, latency_p95), but against a provider
that number is dominated by the provider. What this project itself adds is
the monitoring overhead: embedding extraction, behavioral features, the pooled
and category statistical tests, storage writes and alert generation.

This script measures it with the in-process demo adapter, so generation is
near zero and the cycle wall time is essentially all monitoring machinery.
Cycles run against a copy of the committed seed database, so the reference
windows, matched panels and category histories are exactly production shaped.
The first cycles are warm-up (model load, caches) and are discarded. The
pipeline batches embedding extraction, so per-cycle wall time is the honest
unit; the per-probe figure divides by the panel size and is labeled as
amortized.
"""
import argparse
import csv
import os
import shutil
import tempfile
import time

import numpy as np
import yaml

from monitor import LLMDriftMonitor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_DB = os.path.join(REPO_ROOT, "data", "seed_drift.db")
CONFIG = os.path.join(REPO_ROOT, "config", "config.cloud.yaml")
WARMUP_CYCLES = 3
MEASURED_CYCLES = 12
PROBES_PER_CYCLE = 10


def build_monitor(db_path: str) -> LLMDriftMonitor:
    with open(CONFIG) as f:
        config = yaml.safe_load(f)
    config["data"]["db_path"] = db_path
    config["target"] = {"type": "fake", "model_id": "fake-llm-v1", "seed": 7}
    config_path = os.path.join(tempfile.gettempdir(), "overhead-config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    return LLMDriftMonitor(config_path=config_path)


def run(n_measured: int = MEASURED_CYCLES, out_dir: str = None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(REPO_ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)

    tmp_db = os.path.join(tempfile.gettempdir(), "overhead_measure.db")
    shutil.copyfile(SEED_DB, tmp_db)
    monitor = build_monitor(tmp_db)

    print(f"[overhead] {WARMUP_CYCLES} warm-up cycles, then "
          f"{n_measured} measured cycles on the seed database copy...")

    for _ in range(WARMUP_CYCLES):
        monitor.run_monitoring_cycle(num_probes=PROBES_PER_CYCLE)

    cycle_seconds = []
    for _ in range(n_measured):
        started = time.perf_counter()
        monitor.run_monitoring_cycle(num_probes=PROBES_PER_CYCLE)
        cycle_seconds.append(time.perf_counter() - started)

    ordered = np.sort(cycle_seconds)
    n = len(ordered)
    summary = {
        'n_cycles': n,
        'probes_per_cycle': PROBES_PER_CYCLE,
        'mean_cycle_seconds': round(float(np.mean(ordered)), 4),
        'p50_cycle_seconds': round(float(ordered[(n - 1) // 2]), 4),
        'p95_cycle_seconds': round(float(ordered[int(round(0.95 * (n - 1)))]), 4),
        'mean_per_probe_amortized_seconds': round(
            float(np.mean(ordered)) / PROBES_PER_CYCLE, 4),
        'p95_per_probe_amortized_seconds': round(
            float(ordered[int(round(0.95 * (n - 1)))]) / PROBES_PER_CYCLE, 4),
    }

    csv_path = os.path.join(out_dir, 'overhead_latency.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['cycle', 'cycle_seconds'])
        w.writeheader()
        for i, t in enumerate(cycle_seconds):
            w.writerow({'cycle': i + 1, 'cycle_seconds': round(t, 4)})
        w.writerow({'cycle': 'SUMMARY', 'cycle_seconds': summary['p95_cycle_seconds']})

    print(f"[overhead] mean cycle: {summary['mean_cycle_seconds']}s | "
          f"p50: {summary['p50_cycle_seconds']}s | p95: "
          f"{summary['p95_cycle_seconds']}s | per probe (amortized): "
          f"{summary['p95_per_probe_amortized_seconds']}s")
    print(f"[overhead] Saved to {csv_path}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run("experiment_overhead_seed",
                   experiment_name="llm-drift-experiments",
                   tags={"experiment": "overhead"}) as run_obj:
        log_drift_run(
            run_obj,
            params={
                'experiment': 'overhead', 'n_cycles': n,
                'probes_per_cycle': PROBES_PER_CYCLE,
                'git_hash': get_git_hash(),
            },
            metrics={k: v for k, v in summary.items()
                     if isinstance(v, (int, float)) and k != 'n_cycles'},
            artifact_files=[csv_path]
        )

    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Monitoring overhead per cycle, excluding model inference')
    parser.add_argument('--n-cycles', type=int, default=MEASURED_CYCLES)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(n_measured=args.n_cycles, out_dir=args.out_dir)


if __name__ == '__main__':
    main()
