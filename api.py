"""FastAPI service exposing the LLM drift monitor.

Operational shape, sized for a single small container:

- Reads go through a short TTL cache, so a dashboard polling every few
  seconds (or several visitors at once) costs a handful of queries per
  minute instead of one set per request. The cache is invalidated when a
  run completes, so panels never lag behind a finished cycle.
- Runs are serialized through a one worker queue. Cycles write to SQLite
  and share the embedding model, so running two at once would only add
  latency and memory pressure. POST /runs accepts an Idempotency-Key
  header and replays the original run id for duplicate submissions, and a
  minimum interval between enqueues acts as a simple rate limit.
- The target model gets a circuit breaker: when it fails repeatedly, the
  next cycles fail in milliseconds instead of burning retries.
- MLflow logging runs in a short lived subprocess, keeping mlflow out of
  the serving process. /metrics exposes counters in Prometheus text
  format, and every request is logged with a correlation id.
"""

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


@lru_cache(maxsize=1)
def _get_monitor() -> LLMDriftMonitor:
    """One monitor instance per process.

    Building it costs config parsing, adapter construction, and (after the
    first cycle) a warm embedding model, so every request reusing one instance
    keeps read endpoints and run polling cheap and repeat cycles skip the
    model load entirely.
    """
    return LLMDriftMonitor(config_path=os.environ.get("DRIFT_MONITOR_CONFIG", "config/config.yaml"))


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

_run_queue: "queue.Queue[str]" = queue.Queue(maxsize=4)
MIN_RUN_INTERVAL_SECONDS = 5.0
_last_enqueue = [0.0]
_idempotency: Dict[str, str] = {}
_idempotency_lock = threading.Lock()


def _execute_cycle(run_id: str) -> None:
    """Run a full monitoring cycle and store the outcome."""
    started = time.perf_counter()
    try:
        monitor = _get_monitor()
        result = monitor.run_monitoring_cycle(num_probes=10)
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
    flagged_tests: Optional[list] = None
    ks_statistic: Optional[float] = None
    ks_pvalue: Optional[float] = None
    proportion_z_statistic: Optional[float] = None
    proportion_z_pvalue: Optional[float] = None
    mmd_statistic: Optional[float] = None
    mmd_pvalue: Optional[float] = None
    bh_significant_tests: Optional[list] = None


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


def _compute_latest_drift() -> DriftLatestResponse:
    storage = _get_storage()
    results = storage.get_all_test_results(limit=1)
    latest = results[0] if results else None
    flagged = []
    if latest:
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
    return DriftLatestResponse(
        drift_detected=latest.get('overall_drift_detected') if latest else None,
        flagged_tests=flagged,
        ks_statistic=latest.get('ks_statistic') if latest else None,
        ks_pvalue=latest.get('ks_pvalue') if latest else None,
        proportion_z_statistic=latest.get('proportion_z_stat') if latest else None,
        proportion_z_pvalue=latest.get('proportion_z_pvalue') if latest else None,
        mmd_statistic=latest.get('mmd_statistic') if latest else None,
        mmd_pvalue=latest.get('mmd_pvalue') if latest else None,
        bh_significant_tests=flagged,
    )


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


@app.get("/health")
def health() -> Dict[str, Any]:
    """Check DB and target provider health."""
    db_ok = True
    try:
        storage = _get_storage()
        storage.count_successful_runs()
    except Exception:
        db_ok = False
    try:
        provider_ok = bool(_get_monitor().adapter.ping())
    except Exception:
        provider_ok = False
    status = "ok" if (db_ok and provider_ok) else "degraded"
    return {
        "status": status,
        "db": "ok" if db_ok else "unreachable",
        "provider": _get_monitor().adapter.model_id,
        "provider_status": "ok" if provider_ok else "unreachable",
    }


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
