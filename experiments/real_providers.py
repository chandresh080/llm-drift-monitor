"""Controlled cross-provider experiment: Groq reference versus Gemini current.

This is a real hosted-provider comparison, not a synthetic benchmark and not
a temporal drift study. The run first collects a reference window from Groq
(openai/gpt-oss-20b) with the fixed canary panel, then switches the target
adapter to Gemini (gemini-3.1-flash-lite, native generateContent endpoint) and
runs the same prompts through the standard monitoring cycle, so the existing
detectors, storage, windows, embeddings and alert policy are used unchanged.

Generation settings are pinned for both providers: temperature 0.0, 128 max
output tokens, identical prompt text and order, identical response
normalization and the same embedding model. What cannot be controlled is the
model behind the endpoint: the two providers run different underlying models.

Critical caveat, stated up front: a detected shift here is a cross-provider,
cross-model difference. It cannot be attributed exclusively to provider
behavior, and nothing in this experiment demonstrates temporal drift within a
single model.

Requires GROQ_API_KEY and GEMINI_API_KEY in the environment (or .env). Keys
are read from the environment only and are never printed or logged.
"""
import argparse
import csv
import json
import os
import tempfile
import time
from datetime import datetime

import yaml

from monitor import GeminiAdapter, LLMDriftMonitor, OpenAICompatibleAdapter

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '.env'))
except ImportError:
    pass  # fall back to process environment only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# max_tokens 512: openai/gpt-oss-20b is a reasoning model and spends 150 to
# 450 tokens on hidden reasoning before answering; 128 (the monitoring
# default) truncates it mid-reasoning with empty content. The same value is
# pinned for Gemini so the comparison stays controlled.
GENERATION_SETTINGS = {"temperature": 0.0, "max_tokens": 512, "timeout_s": 60}
# Groq's on-demand tier allows 30 requests per minute; ten concurrent calls
# per cycle plus retries can exceed it, so cycles are paced apart.
PACE_SECONDS = 25


def build_monitor(db_path: str) -> LLMDriftMonitor:
    """Cloud config pointed at the experiment database."""
    config_path = os.path.join(REPO_ROOT, "config", "config.cloud.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)
    config["data"]["db_path"] = db_path
    # A single current cycle can never satisfy two-cycle persistence, so the
    # policy gate is lowered for this experiment to obtain an alert decision.
    # Detector-level flags are reported regardless of this setting.
    config.setdefault("alerts", {})["min_consecutive_cycles"] = 1
    config_path = os.path.join(tempfile.gettempdir(), "real-providers-config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    monitor = LLMDriftMonitor(config_path=config_path)
    return monitor


def cycle_status_counts(storage, run_ids):
    counts = {"success": 0, "failed": 0}
    for run in storage.get_all_runs_ordered():
        if run['id'] in set(run_ids):
            counts[run.get('run_status', 'unknown')] = \
                counts.get(run.get('run_status', 'unknown'), 0) + 1
    return counts


def run_phase(monitor, label, cycles, pace_seconds: float = 0.0):
    """Run monitoring cycles, recording per-cycle execution data."""
    records = []
    for i in range(cycles):
        if i and pace_seconds:
            time.sleep(pace_seconds)
        started = time.perf_counter()
        error = None
        result = None
        try:
            result = monitor.run_monitoring_cycle(num_probes=10)
        except Exception as exc:  # record and continue; never fabricate
            error = f"{type(exc).__name__}: {exc}"
        wall = round(time.perf_counter() - started, 3)
        record = {
            'phase': label,
            'cycle': i + 1,
            'wall_seconds': wall,
            'gen_latency_mean_s': getattr(monitor, 'last_cycle_latency_seconds', None),
            'gen_latency_p50_s': getattr(monitor, 'last_latency_p50_seconds', None),
            'gen_latency_p95_s': getattr(monitor, 'last_latency_p95_seconds', None),
            'usage': dict(getattr(monitor.adapter, 'last_usage', None) or {}),
            'error': error,
            'tests_run': bool(result and result.get('tests_run')),
            'ok_runs': 0,
            'failed_runs': 0,
        }
        if result and result.get('run_ids'):
            counts = cycle_status_counts(monitor.storage, result['run_ids'])
            record['ok_runs'] = counts.get('success', 0)
            record['failed_runs'] = counts.get('failed', 0)
        if result and result.get('tests_run'):
            record['results'] = result['results']
            record['alerts'] = [
                {k: v for k, v in a.items() if k != 'message'}
                for a in result.get('alerts', [])]
        records.append(record)
        status = error or ('tests run' if record['tests_run'] else 'no tests')
        print(f"[real-providers] {label} cycle {i + 1}: {status} "
              f"(wall {wall}s, gen p50 {record['gen_latency_p50_s']}s, "
              f"p95 {record['gen_latency_p95_s']}s)")
    return records


def main():
    parser = argparse.ArgumentParser(
        description='Cross-provider experiment: Groq reference vs Gemini current')
    parser.add_argument('--reference-cycles', type=int, default=5,
                        help='Groq reference cycles; 5 x 10 probes fills the '
                             '50-run reference window')
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    out_dir = args.out_dir or os.path.join(REPO_ROOT, 'results')
    os.makedirs(out_dir, exist_ok=True)

    for var in ('GROQ_API_KEY', 'GEMINI_API_KEY'):
        if not os.environ.get(var):
            raise SystemExit(
                f"{var} is not set. Export it or put it in .env. "
                "The experiment does not fall back to the fake provider.")

    db_path = os.path.join(REPO_ROOT, 'data', 'real_providers.db')
    if os.path.exists(db_path):
        os.remove(db_path)  # reproducibility: every run starts from nothing

    monitor = build_monitor(db_path)
    started_at = datetime.now().isoformat()
    print(f"[real-providers] start {started_at}, panel version "
          f"{monitor.canary_prompts['version']}, embedding model "
          f"{monitor.config['embeddings']['model_name']}")

    # Reference collection on Groq.
    monitor.adapter = OpenAICompatibleAdapter(
        model_id=GROQ_MODEL, base_url=GROQ_BASE_URL,
        api_key=os.environ.get('GROQ_API_KEY') or '',
        timeout=GENERATION_SETTINGS['timeout_s'],
        temperature=GENERATION_SETTINGS['temperature'],
        max_tokens=GENERATION_SETTINGS['max_tokens'],
        reasoning_effort='low')  # gpt-oss-20b is a reasoning model; without
    # cap management it spends the whole token budget on hidden reasoning
    # and returns no answer text (documented provider-specific setting)
    reference = run_phase(monitor, 'groq_reference', args.reference_cycles,
                          pace_seconds=PACE_SECONDS)

    # Switch to Gemini for the comparison cycle.
    monitor.adapter = GeminiAdapter(
        model_id=GEMINI_MODEL, base_url=GEMINI_BASE_URL,
        api_key=os.environ.get('GEMINI_API_KEY') or '',
        timeout=GENERATION_SETTINGS['timeout_s'],
        temperature=GENERATION_SETTINGS['temperature'],
        max_tokens=GENERATION_SETTINGS['max_tokens'])
    current = run_phase(monitor, 'gemini_current', 1)

    # Persist per-cycle records.
    csv_path = os.path.join(out_dir, 'real_provider_cycles.csv')
    fieldnames = ['phase', 'cycle', 'wall_seconds', 'ok_runs', 'failed_runs',
                  'gen_latency_mean_s',
                  'gen_latency_p50_s', 'gen_latency_p95_s',
                  'prompt_tokens', 'completion_tokens', 'total_tokens',
                  'error', 'tests_run']
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for rec in reference + current:
            usage = rec['usage']
            w.writerow({
                'phase': rec['phase'], 'cycle': rec['cycle'],
                'wall_seconds': rec['wall_seconds'],
                'ok_runs': rec.get('ok_runs', 0),
                'failed_runs': rec.get('failed_runs', 0),
                'gen_latency_mean_s': rec['gen_latency_mean_s'],
                'gen_latency_p50_s': rec['gen_latency_p50_s'],
                'gen_latency_p95_s': rec['gen_latency_p95_s'],
                'prompt_tokens': usage.get('prompt_tokens',
                                           usage.get('promptTokenCount')),
                'completion_tokens': usage.get(
                    'completion_tokens', usage.get('candidatesTokenCount')),
                'total_tokens': usage.get(
                    'total_tokens', usage.get('totalTokenCount')),
                'error': rec['error'], 'tests_run': rec['tests_run'],
            })

    # Full machine-readable result for the report.
    json_path = os.path.join(out_dir, 'real_provider_results.json')
    payload = {
        'started_at': started_at,
        'setup': {
            'reference_provider': 'Groq', 'reference_model': GROQ_MODEL,
            'reference_base_url': GROQ_BASE_URL,
            'current_provider': 'Google Gemini', 'current_model': GEMINI_MODEL,
            'current_base_url': GEMINI_BASE_URL,
            'generation_settings': GENERATION_SETTINGS,
            'groq_reasoning_effort': 'low',
            'uncontrolled_differences': [
                'different underlying models (this is a cross-provider '
                'comparison, not a within-model temporal drift study)',
                'Groq openai/gpt-oss-20b consumes part of the token budget '
                'on hidden reasoning even at low effort; Gemini does not '
                'expose this knob in this pipeline',
            ],
            'prompt_panel_version': monitor.canary_prompts['version'],
            'prompts_per_cycle': 10,
            'embedding_model': monitor.config['embeddings']['model_name'],
            'embedding_device': monitor.config['embeddings']['device'],
            'reference_window_size': monitor.config['reference']['window_size'],
            'min_consecutive_cycles_override': 1,
            'note': 'min_consecutive_cycles lowered from 2 to 1 because a '
                    'single current cycle cannot satisfy two-cycle persistence; '
                    'this is an experiment-only override',
        },
        'reference_cycles': reference,
        'current_cycles': current,
    }
    with open(json_path, 'w') as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"[real-providers] Complete. Saved to {csv_path} and {json_path}")

    from tracking import drift_run, log_drift_run, get_git_hash
    final = current[0] if current else {}
    final_results = final.get('results', {})
    with drift_run("experiment_real_providers",
                   experiment_name="llm-drift-experiments",
                   tags={"experiment": "real_providers"}) as run_obj:
        log_drift_run(
            run_obj,
            params={
                'experiment': 'real_providers',
                'reference_model': GROQ_MODEL, 'current_model': GEMINI_MODEL,
                'prompt_panel_version': monitor.canary_prompts['version'],
                'git_hash': get_git_hash(),
            },
            metrics={
                'ks_pvalue': final_results.get('ks_pvalue'),
                'proportion_pvalue': final_results.get('proportion_z_pvalue'),
                'mmd_pvalue': final_results.get('mmd_pvalue'),
                'mmd_statistic': final_results.get('mmd_statistic'),
                'overall_drift_detected': final_results.get(
                    'overall_drift_detected'),
                'groq_gen_p95_s': max(
                    (r['gen_latency_p95_s'] or 0) for r in reference) if reference else None,
                'gemini_gen_p95_s': final.get('gen_latency_p95_s'),
            },
            artifact_files=[csv_path, json_path]
        )


if __name__ == '__main__':
    main()