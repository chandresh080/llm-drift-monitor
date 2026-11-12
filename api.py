"""FastAPI service exposing the LLM drift monitor.

Operational shape, sized for a single small container:

- Reads go through a short TTL cache, so a dashboard polling every few
  seconds (or several visitors at once) costs a handful of queries per
  minute instead of one set per request. The cache is invalidated when a
  run completes, so panels never lag behind a finished cycle.
- Runs are serialized through a one worker queue. Cycles write to SQLite
  and share the embedding model, so running two at once would only add
  latency and memory pressure. POST /runs accepts an Idempotency-Key
  header and replays the original run id for duplicate submissions, a
  minimum interval between enqueues acts as a global rate limit, and the
  queue is deliberately shallow (2) so a burst hits an explicit
  queue-full 429 instead of accumulating runtime. The public demo adds
  per visitor and daily budget limits on top (see the throttle constants).
- The target model gets a circuit breaker: when it fails repeatedly, the
  next cycles fail in milliseconds instead of burning retries.
- MLflow logging runs in a short lived subprocess, keeping mlflow out of
  the serving process. /metrics exposes counters in Prometheus text
  format, and every request is logged with a correlation id.
"""

import csv
import json
import os
import queue
import resource
import threading
import time
import uuid
import logging
from functools import lru_cache
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from monitor import LLMDriftMonitor
from storage import DriftMonitorStorage

app = FastAPI(title="LLM Drift Monitor")

_runs: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger("llm_drift_monitor.api")

# ---------- process level service object ----------

# One build at a time: on a cold start several dashboard endpoints can miss
# the (un populated) cache simultaneously, and without a lock each request
# would construct its own monitor instance, each paying the config parse and
# adapter setup cost. The lock turns the race into a queue.
_monitor_lock = threading.Lock()


@lru_cache(maxsize=1)
def _build_monitor() -> LLMDriftMonitor:
    return LLMDriftMonitor(config_path=os.environ.get("DRIFT_MONITOR_CONFIG", "config/config.yaml"))


def _get_monitor() -> LLMDriftMonitor:
    """One monitor instance per process.

    Building it costs config parsing, adapter construction, and (after the
    first cycle) a warm embedding model, so every request reusing one instance
    keeps read endpoints and run polling cheap and repeat cycles skip the
    model load entirely.
    """
    with _monitor_lock:
        return _build_monitor()


def _get_storage() -> DriftMonitorStorage:
    return _get_monitor().storage


# ---------- metrics ----------

_metrics: Dict[str, Any] = {
    "cycles_total": 0,
    "cycles_failed_total": 0,
    "last_cycle_seconds": None,
    "read_cache_hits_total": 0,
    "read_cache_misses_total": 0,
    "runs_rejected_total": 0,
    "runs_replayed_total": 0,
}
_started_at = time.time()


# ---------- read cache ----------

READ_CACHE_TTL_SECONDS = 5.0
_read_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def cached_read(key: str, producer) -> Any:
    """Return a cached value for a few seconds, or compute and store it.

    Panel polling and concurrent visitors collapse into one set of queries
    per TTL window; a completed run clears the cache so data stays honest.
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _read_cache.get(key)
        if entry is not None and entry[0] > now:
            _metrics["read_cache_hits_total"] += 1
            return entry[1]
    value = producer()
    with _cache_lock:
        _read_cache[key] = (time.monotonic() + READ_CACHE_TTL_SECONDS, value)
        _metrics["read_cache_misses_total"] += 1
    return value


def invalidate_read_cache() -> None:
    with _cache_lock:
        _read_cache.clear()


# ---------- embedding model idle release ----------

# The model is kept warm so back to back cycles skip its load time, but it
# is dropped after a quiet period: the free hosting tier has a 512 MiB
# budget, and a model parked in memory between rare visits wastes most of it.
MODEL_IDLE_RELEASE_SECONDS = 300
_release_lock = threading.Lock()
_release_timer: Optional[threading.Timer] = None


def _release_embedding_model() -> None:
    try:
        _get_monitor().embedder.release()
    except Exception:
        logger.exception("releasing the embedding model failed")


def _schedule_model_release() -> None:
    global _release_timer
    with _release_lock:
        if _release_timer is not None:
            _release_timer.cancel()
        timer = threading.Timer(MODEL_IDLE_RELEASE_SECONDS, _release_embedding_model)
        timer.daemon = True
        _release_timer = timer
        timer.start()


# ---------- run queue (single worker; cycles are serialized on purpose) ----------

_run_queue: "queue.Queue[str]" = queue.Queue(maxsize=2)
MIN_RUN_INTERVAL_SECONDS = 5.0
_last_enqueue = [0.0]

# Public demo throttle. Each cycle spends real Gemini quota and does 40 live
# model calls, so a visitor driven rate limit sits on top of the global
# interval: one run per IP per PUBLIC_RUN_INTERVAL_SECONDS, and a shared
# daily cycle budget so a scripted loop cannot drain the free tier. Both
# limits are process local and reset on restart; that is acceptable for a
# demo where the point is bounded spend, not adversarial accounting.
PUBLIC_RUN_INTERVAL_SECONDS = 600.0
DAILY_CYCLE_BUDGET = 15
_ip_last_run: Dict[str, float] = {}
_ip_lock = threading.Lock()
_daily_lock = threading.Lock()
_daily_usage = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "count": 0}


def _daily_count_and_date() -> tuple:
    with _daily_lock:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if _daily_usage["date"] != today:
            _daily_usage["date"] = today
            _daily_usage["count"] = 0
        return _daily_usage["count"], today


def _seconds_until_utc_midnight() -> int:
    return max(60, int(86400 - (time.time() % 86400)))


def _client_ip(request: Request) -> str:
    # Render terminates TLS on a proxy, so the socket peer is the proxy and
    # the real visitor address arrives in X-Forwarded-For's first hop.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _seconds_until_next_ip_run(ip: str) -> float:
    with _ip_lock:
        last = _ip_last_run.get(ip)
        now = time.monotonic()
        if last is None or (now - last) >= PUBLIC_RUN_INTERVAL_SECONDS:
            return 0.0
        return PUBLIC_RUN_INTERVAL_SECONDS - (now - last)
_idempotency: Dict[str, str] = {}
_idempotency_lock = threading.Lock()


def _execute_cycle(run_id: str) -> None:
    """Run a full monitoring cycle and store the outcome."""
    started = time.perf_counter()
    try:
        monitor = _get_monitor()
        # Full configured panel, not a reduced subset: the committed reference
        # baseline and the matched window rule both require every stored cycle
        # to cover the same prompt ids, so a smaller visitor cycle could never
        # share a window with the seeded history and every check would stop at
        # "insufficient matched window data". The 13 s call spacing and the
        # visitor/daily budgets keep the free tier cost bounded instead.
        result = monitor.run_monitoring_cycle()
        _runs[run_id] = {"status": "completed", "result": result}
        _metrics["cycles_total"] += 1
        _metrics["last_cycle_seconds"] = round(time.perf_counter() - started, 2)
        invalidate_read_cache()
        logger.info(json.dumps({
            "event": "drift_run_completed",
            "run_id": run_id,
            "status": "completed",
            "tests_run": result.get('tests_run'),
            "duration_s": _metrics["last_cycle_seconds"],
        }))
    except Exception as exc:
        _runs[run_id] = {"status": "failed", "error": str(exc)}
        _metrics["cycles_failed_total"] += 1
        logger.error(json.dumps({"event": "drift_run_failed", "run_id": run_id, "error": str(exc)}))
    finally:
        _schedule_model_release()


def _cycle_worker() -> None:
    while True:
        run_id = _run_queue.get()
        try:
            _execute_cycle(run_id)
        finally:
            _run_queue.task_done()


threading.Thread(target=_cycle_worker, daemon=True, name="drift-cycle-worker").start()


# ---------- request correlation and timing ----------


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a correlation id to every request and log one structured line."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(json.dumps({
        "event": "http_request",
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }))
    return response


# ---------- schemas ----------


class RunResponse(BaseModel):
    run_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class DriftLatestResponse(BaseModel):
    drift_detected: Optional[int] = None
    distribution_shift_detected: Optional[int] = None
    quality_change_detected: Optional[int] = None
    flagged_tests: Optional[list] = None
    quality_degradation: Optional[int] = None
    timestamp: Optional[str] = None
    reference_size: Optional[int] = None
    current_size: Optional[int] = None
    ks_statistic: Optional[float] = None
    ks_pvalue: Optional[float] = None
    ks_adjusted_pvalue: Optional[float] = None
    ks_significant: Optional[int] = None
    proportion_z_statistic: Optional[float] = None
    proportion_z_pvalue: Optional[float] = None
    proportion_z_adjusted_pvalue: Optional[float] = None
    proportion_z_significant: Optional[int] = None
    mmd_statistic: Optional[float] = None
    mmd_pvalue: Optional[float] = None
    mmd_adjusted_pvalue: Optional[float] = None
    mmd_significant: Optional[int] = None
    bh_significant_tests: Optional[list] = None
    length_shift_pct: Optional[float] = None
    centroid_distance: Optional[float] = None
    refusal_rate_reference: Optional[float] = None
    refusal_rate_current: Optional[float] = None
    unique_reference: Optional[int] = None
    unique_current: Optional[int] = None
    paired_cluster_mmd_statistic: Optional[float] = None
    paired_cluster_mmd_pvalue: Optional[float] = None
    paired_cluster_prompt_pairs: Optional[int] = None
    paired_cluster_method: Optional[str] = None
    mmd_permutation_unit: Optional[str] = None
    provider_latency_mean_seconds: Optional[float] = None
    provider_latency_p50_seconds: Optional[float] = None
    provider_latency_p95_seconds: Optional[float] = None
    probe_tokens_total: Optional[int] = None


class ProbeRunResponse(BaseModel):
    id: Optional[int] = None
    timestamp: Optional[str] = None
    prompt_version: Optional[str] = None
    prompt_text: Optional[str] = None
    model_id: Optional[str] = None
    response_length: Optional[int] = None
    is_valid: Optional[int] = None
    run_status: Optional[str] = None
    adapter_type: Optional[str] = None


class AlertResponse(BaseModel):
    timestamp: Optional[str] = None
    alert_type: Optional[str] = None
    severity: Optional[str] = None
    message: Optional[str] = None
    overall_drift_detected: Optional[int] = None
    overall_quality_degradation: Optional[int] = None


# ---------- endpoints ----------


@app.post("/runs", response_model=RunResponse)
async def create_run(request: Request) -> RunResponse:
    """Enqueue a probe + detection run; the result is polled via /runs/{id}.

    A duplicate submission with the same Idempotency-Key returns the
    original run instead of starting a second cycle.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if idem_key:
        with _idempotency_lock:
            existing = _idempotency.get(idem_key)
        if existing and existing in _runs:
            _metrics["runs_replayed_total"] += 1
            run = _runs[existing]
            return RunResponse(run_id=existing, status=run.get("status", "unknown"))

    now = time.monotonic()
    elapsed = now - _last_enqueue[0]
    if elapsed < MIN_RUN_INTERVAL_SECONDS:
        _metrics["runs_rejected_total"] += 1
        retry_after = max(1, int(MIN_RUN_INTERVAL_SECONDS - elapsed)) + 1
        raise HTTPException(
            status_code=429,
            detail=f"A check was started less than {MIN_RUN_INTERVAL_SECONDS:.0f}s ago. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )

    # Per visitor interval: one cycle per IP per the public demo interval.
    # Chinese checked first so the more specific message wins when both
    # limits would trip on the same submission.
    client_ip = _client_ip(request)
    ip_wait = _seconds_until_next_ip_run(client_ip)
    if ip_wait > 0:
        _metrics["runs_rejected_total"] += 1
        raise HTTPException(
            status_code=429,
            detail=(
                "Each visitor can start one check every "
                f"{PUBLIC_RUN_INTERVAL_SECONDS:.0f}s. Try again later."),
            headers={"Retry-After": str(int(ip_wait) + 1)},
        )

    # Shared daily cycle budget across all visitors on this deployment.
    used_today, budget_date = _daily_count_and_date()
    if used_today >= DAILY_CYCLE_BUDGET:
        _metrics["runs_rejected_total"] += 1
        raise HTTPException(
            status_code=429,
            detail=(
                f"The demo's daily cycle budget ({DAILY_CYCLE_BUDGET} for "
                f"{budget_date}) is used up. It resets at UTC midnight."),
            headers={"Retry-After": str(_seconds_until_utc_midnight())},
        )

    run_id = str(uuid.uuid4())
    # Register before enqueueing: the worker may finish a warm cycle in well
    # under a second, and a late "running" write must never clobber a result.
    _runs[run_id] = {"status": "running"}
    try:
        _run_queue.put_nowait(run_id)
    except queue.Full:
        del _runs[run_id]
        _metrics["runs_rejected_total"] += 1
        raise HTTPException(status_code=429, detail="The run queue is full. Try again shortly.")

    _last_enqueue[0] = now
    # Budget and per IP accounting only after the cycle is actually accepted;
    # rejected submissions consume neither.
    with _ip_lock:
        _ip_last_run[client_ip] = now
        # Bounded: forget stale entries so the map cannot grow with visitors.
        if len(_ip_last_run) > 10000:
            cutoff = now - PUBLIC_RUN_INTERVAL_SECONDS
            for stale_ip, stale_at in list(_ip_last_run.items()):
                if stale_at < cutoff:
                    _ip_last_run.pop(stale_ip, None)
    with _daily_lock:
        _daily_usage["count"] += 1
    if idem_key:
        with _idempotency_lock:
            _idempotency[idem_key] = run_id
            # Bounded: drop the oldest keys so the map cannot grow forever.
            while len(_idempotency) > 1000:
                _idempotency.pop(next(iter(_idempotency)))
    return RunResponse(run_id=run_id, status="running")


@app.get("/runs", response_model=list[ProbeRunResponse])
def list_runs(limit: int = 100) -> list[ProbeRunResponse]:
    """List recent probe runs."""
    runs = cached_read(f"runs:{limit}", lambda: _get_storage().get_all_runs(limit=limit))
    return [ProbeRunResponse(**run) for run in runs]


@app.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str) -> RunResponse:
    """Return status and results for a run."""
    run = _runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(
        run_id=run_id,
        status=run.get("status", "unknown"),
        result=run.get("result"),
        error=run.get("error"),
    )


@app.get("/alerts", response_model=list[AlertResponse])
def list_alerts(limit: int = 100) -> list[AlertResponse]:
    """List recent alerts."""
    alerts = cached_read(f"alerts:{limit}", lambda: _get_storage().get_all_alerts(limit=limit))
    return [AlertResponse(**alert) for alert in alerts]


@app.get("/drift/latest", response_model=DriftLatestResponse)
def get_latest_drift() -> DriftLatestResponse:
    """Return the current drift status and flagged tests."""
    return cached_read("drift_latest", _compute_latest_drift)


def compute_latest_fields(latest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map one stored test_results row onto the /drift/latest response fields.

    A note on names: a cycle's results dict uses results oriented keys
    (ref_refusal_rate, reference_unique_responses), storage stores them in
    display oriented columns (refusal_rate_reference, unique_reference) and
    this endpoint returns the columns. The mapping lives in exactly one
    place, storage.insert_test_results writes the columns, and this function
    reads them back, so a rename in one layer shows up as a test failure
    rather than an empty dashboard field.
    """
    if not latest:
        return {
            'drift_detected': None, 'flagged_tests': [], 'quality_degradation': None,
            'bh_significant_tests': [],
            'distribution_shift_detected': None, 'quality_change_detected': None,
            'timestamp': None, 'reference_size': None, 'current_size': None,
            'ks_statistic': None, 'ks_pvalue': None, 'ks_adjusted_pvalue': None,
            'ks_significant': None,
            'proportion_z_statistic': None, 'proportion_z_pvalue': None,
            'proportion_z_adjusted_pvalue': None, 'proportion_z_significant': None,
            'mmd_statistic': None, 'mmd_pvalue': None, 'mmd_adjusted_pvalue': None,
            'mmd_significant': None,
            'length_shift_pct': None, 'centroid_distance': None,
            'refusal_rate_reference': None, 'refusal_rate_current': None,
            'unique_reference': None, 'unique_current': None,
            'paired_cluster_mmd_statistic': None, 'paired_cluster_mmd_pvalue': None,
            'paired_cluster_prompt_pairs': None,
            'paired_cluster_method': None, 'mmd_permutation_unit': None,
            'provider_latency_mean_seconds': None,
            'provider_latency_p50_seconds': None,
            'provider_latency_p95_seconds': None,
            'probe_tokens_total': None,
        }
    raw = latest.get('bh_significant_tests')
    if isinstance(raw, str):
        try:
            flagged = json.loads(raw)
        except Exception:
            flagged = []
    elif raw is None:
        flagged = []
    else:
        flagged = raw
    return {
        'drift_detected': None if latest.get('overall_drift_detected') is None
            else int(latest['overall_drift_detected']),
        # Split signal groups, derived here from the stored per test flags so
        # legacy rows without monitor level fields still read correctly. A
        # validity flag can never drive the distribution verdict.
        'distribution_shift_detected': (
            None if latest.get('ks_significant') is None
            and latest.get('mmd_significant') is None
            else int(bool(latest.get('ks_significant'))
                     or bool(latest.get('mmd_significant')))),
        'quality_change_detected': (
            None if latest.get('proportion_z_significant') is None
            else int(latest['proportion_z_significant'])),
        'flagged_tests': flagged,
        # Served under both names: flagged_tests is the canonical field the
        # dashboard reads; bh_significant_tests stays for API consumers of
        # the original schema.
        'bh_significant_tests': flagged,
        'quality_degradation': None if latest.get('overall_quality_degradation') is None
            else int(latest['overall_quality_degradation']),
        'timestamp': latest.get('timestamp'),
        'reference_size': latest.get('reference_size'),
        'current_size': latest.get('current_size'),
        'ks_statistic': latest.get('ks_statistic'),
        'ks_pvalue': latest.get('ks_pvalue'),
        'ks_adjusted_pvalue': latest.get('ks_adjusted_pvalue'),
        'ks_significant': latest.get('ks_significant'),
        'proportion_z_statistic': latest.get('proportion_z_stat'),
        'proportion_z_pvalue': latest.get('proportion_z_pvalue'),
        'proportion_z_adjusted_pvalue': latest.get('proportion_z_adjusted_pvalue'),
        'proportion_z_significant': latest.get('proportion_z_significant'),
        'mmd_statistic': latest.get('mmd_statistic'),
        'mmd_pvalue': latest.get('mmd_pvalue'),
        'mmd_adjusted_pvalue': latest.get('mmd_adjusted_pvalue'),
        'mmd_significant': latest.get('mmd_significant'),
        'length_shift_pct': latest.get('length_shift_pct'),
        'centroid_distance': latest.get('centroid_distance'),
        'refusal_rate_reference': latest.get('refusal_rate_reference'),
        'refusal_rate_current': latest.get('refusal_rate_current'),
        'unique_reference': latest.get('unique_reference'),
        'unique_current': latest.get('unique_current'),
        'paired_cluster_mmd_statistic': latest.get('paired_cluster_mmd_statistic'),
        'paired_cluster_mmd_pvalue': latest.get('paired_cluster_mmd_pvalue'),
        'paired_cluster_prompt_pairs': latest.get('paired_cluster_prompt_pairs'),
        'paired_cluster_method': latest.get('paired_cluster_method'),
        'mmd_permutation_unit': latest.get('mmd_permutation_unit'),
        'provider_latency_mean_seconds': latest.get('provider_latency_mean_seconds'),
        'provider_latency_p50_seconds': latest.get('provider_latency_p50_seconds'),
        'provider_latency_p95_seconds': latest.get('provider_latency_p95_seconds'),
        'probe_tokens_total': latest.get('probe_tokens_total'),
    }


def _compute_latest_drift() -> DriftLatestResponse:
    storage = _get_storage()
    results = storage.get_all_test_results(limit=1)
    latest = results[0] if results else None
    return DriftLatestResponse(**compute_latest_fields(latest))


@app.get("/drift/results")
def get_drift_results(limit: int = 20) -> list:
    """List recent drift test results."""
    return cached_read(f"results:{limit}", lambda: _get_storage().get_all_test_results(limit=limit))


@app.get("/stats")
def get_stats() -> Dict[str, Any]:
    """Exact counts for the dashboard headers."""
    return cached_read("stats", lambda: {
        "probe_runs": _get_storage().count_all_runs(),
        "test_results": _get_storage().count_test_results(),
        "alerts": _get_storage().count_alerts(),
    })


@app.get("/config")
def get_config() -> Dict[str, Any]:
    """Monitoring configuration the dashboard labels itself with.

    Read only configuration that shapes what a visitor sees; secrets and
    provider keys are never included. Provider availability is reported
    explicitly: a missing or refused provider key means the demo serves
    stored data and states that new cycles are unavailable, it never falls
    back to a synthetic adapter behind the scenes.
    """
    monitor = _get_monitor()
    cfg = monitor.config
    adapter = monitor.adapter
    def build() -> Dict[str, Any]:
        return {
            "target_model": adapter.model_id if adapter else None,
            "adapter_type": type(adapter).__name__ if adapter else None,
            "provider_available": adapter is not None,
            "provider_error": monitor.adapter_error,
            "embedding_model": cfg['embeddings']['model_name'].split('/')[-1],
            "reference_window": cfg['reference']['window_size'],
            "current_window": cfg['monitoring']['batch_size'],
            "tests_start_at_responses": cfg['reference']['window_size']
            + cfg['monitoring']['batch_size'],
            "prompt_panel_version": monitor.canary_prompts['version'],
            "prompt_panel_probes": len(monitor.canary_prompts['prompts']),
            "probes_per_cycle": cfg['monitoring']['batch_size'],
            "alpha": cfg['alerts'].get('significance_threshold', 0.05),
            "min_consecutive_cycles": cfg['alerts'].get(
                'min_consecutive_cycles', 2),
            "category_window_cycles": cfg['monitoring'].get(
                'category_window_cycles', 5),
            "min_samples_per_category": cfg['monitoring'].get(
                'min_samples_per_category', 8),
            # Baseline manifest: what actually produced the reference window
            # stored in this database, so the comparison shown to a visitor
            # carries its own provenance.
            "baseline_manifest": monitor.storage.get_manifest() or None,
            # Run policy as served: the same numbers the throttle enforces,
            # so the frontend can state to visitors when their next check is.
            "run_policy": {
                "per_visitor_interval_seconds": PUBLIC_RUN_INTERVAL_SECONDS,
                "daily_cycle_budget": DAILY_CYCLE_BUDGET,
                "daily_cycles_used_today": _daily_usage["count"],
            },
        }
    return cached_read("config", build)


@app.get("/drift/comparison")
def get_comparison() -> Dict[str, Any]:
    """Reference versus current answers, prompt by prompt, for inspection.

    What the detectors compared: the matched panel's prompts and both
    windows' stored answers with their validity, refusal flag and length.
    """
    return cached_read("comparison", _get_monitor().get_window_comparison)


@app.get("/drift/categories")
def get_categories() -> Dict[str, Any]:
    """Category family state: tested categories with adjusted p values,
    skipped categories with their distinct sample counts."""
    return cached_read("categories", _get_monitor().get_category_localization)


@app.get("/evaluation")
def get_evaluation() -> Dict[str, Any]:
    """Committed simulation results, summarized for the evaluation tab.

    Reads the experiments' committed CSV outputs. These numbers come from
    controlled simulations, not from the live demo service. Missing files
    degrade gracefully to null fields instead of failing the dashboard.
    """
    def read_rows(name: str) -> list:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "results", name)
        if not os.path.exists(path):
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    def rule_row(rows: list, rule: str) -> Optional[Dict[str, str]]:
        for r in rows:
            if r.get('rule') == rule and float(r.get('alpha', 0) or 0) == 0.05:
                return r
        return None

    delay = read_rows("delay_detection_delay.csv")
    alarms = read_rows("delay_false_alarms.csv")
    default = "persist2"  # the shipped alert policy: two-cycle persistence
    d_row = rule_row(delay, default) or (delay[0] if delay else None)
    a_row = rule_row(alarms, default) or (alarms[0] if alarms else None)

    overhead = read_rows("overhead_latency.csv")
    overhead_p95 = None
    overhead_samples = 0
    if overhead:
        # Timing samples only. The benchmark's SUMMARY row restates the p95
        # statistic, not an independent observation, so it is excluded here
        # and the p95 is recomputed from the raw cycles with the same
        # round(0.95 * (n - 1)) index rule experiments/overhead.py used to
        # produce it. The sample count is reported next to the number
        # because a percentile over a dozen cycles is an order of magnitude
        # hint, not a latency guarantee.
        seconds = sorted(float(r['cycle_seconds']) for r in overhead
                         if r.get('cycle_seconds')
                         and r.get('cycle', '').upper() != 'SUMMARY')
        overhead_samples = len(seconds)
        if seconds:
            overhead_p95 = seconds[round(0.95 * (overhead_samples - 1))]

    tradeoff = []
    for rule in ("single", "kofn", "persist2", "cusum"):
        d = rule_row(delay, rule)
        a = rule_row(alarms, rule)
        if d:
            tradeoff.append({
                "rule": rule,
                "mean_detection_delay_days": d.get(
                    'mean_detection_delay_days'),
                "detection_rate_pct": d.get('detection_rate_pct'),
                "false_alarms_per_100_days": (a or {}).get(
                    'false_alarms_per_100_days'),
            })

    power = [r for r in read_rows("power_curves.csv") if r.get('power')]
    power_mmd = [r for r in power if r.get('test') == 'mmd' and r.get('n') == '10']
    power_ks = [r for r in power if r.get('test') == 'ks' and r.get('n') == '10']
    # Every power point is estimated from a fixed number of simulation
    # trials; report it so a 100% detection rate can be read as "100 out of
    # these evaluated trials", not as a guarantee.
    power_trials = None
    if power_mmd and power_mmd[0].get('n_sims'):
        power_trials = int(power_mmd[0]['n_sims'])

    reliability = [r for r in read_rows("reliability_results.csv")
                   if r.get('row_type') == 'condition']

    return {
        "headline": {
            "mean_detection_delay_days": (d_row or {}).get(
                'mean_detection_delay_days'),
            "detection_rate_pct": (d_row or {}).get('detection_rate_pct'),
            "evaluated_shift_sigma": (d_row or {}).get(
                'shift_magnitude_sigma'),
            "false_alarms_per_100_days": (a_row or {}).get(
                'false_alarms_per_100_days'),
            "overhead_p95_seconds": (
                round(overhead_p95, 3) if overhead_p95 is not None else None),
            "overhead_samples": overhead_samples,
            "default_rule": default,
        },
        "tradeoff": tradeoff,
        "power_curves": {
            "mmd_n10": power_mmd,
            "ks_n10": power_ks,
            "trials_per_point": power_trials,
        },
        "reliability_conditions": reliability,
        "source": "Simulation runs committed under results/; experiments: "
                  "delay.py (detection delay and false alarms), power.py "
                  "(power curves), overhead.py (cycle overhead), and "
                  "reliability.py (distribution shift versus quality "
                  "regression). Not live production performance.",
    }


@app.get("/health/live")
def liveness() -> Dict[str, str]:
    """Liveness: is this process serving. Returns without touching the
    monitor, storage or any model; a healthy process answers in
    milliseconds even while the target model is still loading. Platform
    health checks and dashboards use this; dependency readiness is
    reported separately by /health."""
    return {"status": "ok"}


@app.get("/health")
def health() -> Dict[str, Any]:
    """Readiness: DB and target provider status.

    Deliberately heavier than liveness: it initializes the monitor so the
    first /runs call does not pay the whole cost, and it asks the adapter
    for a real ping. Dashboard panels do not wait on this; they read stored
    data first and treat this as status context.
    """
    db_ok = True
    try:
        storage = _get_storage()
        storage.count_successful_runs()
    except Exception:
        db_ok = False
    monitor = _get_monitor()
    provider_name = (monitor.adapter.model_id if monitor.adapter
                     else cfg_model_id(monitor))
    if monitor.adapter is None:
        # Explicit unavailable state, never a synthetic fallback: the
        # deployment is missing its provider key and states it.
        provider_ok = False
        provider_error = monitor.adapter_error
        provider_status = "not_configured"
    else:
        try:
            provider_ok = bool(monitor.adapter.ping())
            provider_error = None if provider_ok else \
                "Provider ping failed."
            provider_status = "ok" if provider_ok else "unreachable"
        except Exception as exc:
            provider_ok = False
            provider_error = str(exc)
            provider_status = "unreachable"
    status = "ok" if (db_ok and provider_ok) else "degraded"
    return {
        "status": status,
        "db": "ok" if db_ok else "unreachable",
        "provider": provider_name,
        "provider_status": provider_status,
        "provider_error": provider_error,
    }


def cfg_model_id(monitor: LLMDriftMonitor) -> Optional[str]:
    """Configured provider model id, readable even when no adapter exists."""
    return monitor.config.get('target', {}).get('model_id')


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Counters for the service itself, in Prometheus text format."""
    try:
        peak_rss_mb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    except Exception:
        peak_rss_mb = 0
    model_loaded = _get_monitor().embedder._model is not None
    lines = [
        "# HELP drift_cycles_total Monitoring cycles completed.",
        "# TYPE drift_cycles_total counter",
        f"drift_cycles_total {_metrics['cycles_total']}",
        "# HELP drift_cycles_failed_total Monitoring cycles that failed.",
        "# TYPE drift_cycles_failed_total counter",
        f"drift_cycles_failed_total {_metrics['cycles_failed_total']}",
        "# HELP drift_last_cycle_seconds Duration of the most recent cycle.",
        "# TYPE drift_last_cycle_seconds gauge",
        f"drift_last_cycle_seconds {_metrics['last_cycle_seconds'] or 0}",
        "# HELP drift_queue_depth Cycles waiting in the run queue.",
        "# TYPE drift_queue_depth gauge",
        f"drift_queue_depth {_run_queue.qsize()}",
        "# HELP drift_runs_rejected_total Run submissions rejected by the rate limit or a full queue.",
        "# TYPE drift_runs_rejected_total counter",
        f"drift_runs_rejected_total {_metrics['runs_rejected_total']}",
        "# HELP drift_runs_replayed_total Run submissions replayed via an idempotency key.",
        "# TYPE drift_runs_replayed_total counter",
        f"drift_runs_replayed_total {_metrics['runs_replayed_total']}",
        "# HELP drift_read_cache_hits_total Read endpoint responses served from the TTL cache.",
        "# TYPE drift_read_cache_hits_total counter",
        f"drift_read_cache_hits_total {_metrics['read_cache_hits_total']}",
        "# HELP drift_read_cache_misses_total Read endpoint responses computed fresh.",
        "# TYPE drift_read_cache_misses_total counter",
        f"drift_read_cache_misses_total {_metrics['read_cache_misses_total']}",
        "# HELP drift_embedding_model_loaded Whether the embedding model is held in memory.",
        "# TYPE drift_embedding_model_loaded gauge",
        f"drift_embedding_model_loaded {1 if model_loaded else 0}",
        "# HELP drift_peak_rss_mb Peak resident memory of the process.",
        "# TYPE drift_peak_rss_mb gauge",
        f"drift_peak_rss_mb {peak_rss_mb}",
        "# HELP drift_uptime_seconds Seconds since the process started.",
        "# TYPE drift_uptime_seconds gauge",
        f"drift_uptime_seconds {int(time.time() - _started_at)}",
    ]
    return "\n".join(lines) + "\n"


# ---------- dashboard UI (served from the same origin; API stays untouched) ----------

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

if os.path.isdir(_WEB_DIR):

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(_WEB_DIR, "index.html"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/static/{asset_name}", include_in_schema=False)
    def static_asset(asset_name: str):
        return FileResponse(
            os.path.join(_WEB_DIR, os.path.basename(asset_name)),
            headers={"Cache-Control": "no-store"},
        )
