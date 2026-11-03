"""FastAPI service exposing the LLM drift monitor."""

import json
import os
import uuid
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from monitor import LLMDriftMonitor
from storage import DriftMonitorStorage

app = FastAPI(title="LLM Drift Monitor")

_runs: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger("llm_drift_monitor.api")


def _get_monitor() -> LLMDriftMonitor:
    return LLMDriftMonitor(config_path=os.environ.get("DRIFT_MONITOR_CONFIG", "config.yaml"))


def _get_storage() -> DriftMonitorStorage:
    return _get_monitor().storage


def _run_monitor_task(run_id: str) -> None:
    """Background task: run a full monitoring cycle and store the outcome."""
    try:
        monitor = _get_monitor()
        result = monitor.run_monitoring_cycle(num_probes=10)
        _runs[run_id] = {"status": "completed", "result": result}
        logger.info(json.dumps({
            "event": "drift_run_completed",
            "run_id": run_id,
            "status": "completed",
            "tests_run": result.get('tests_run'),
        }))
    except Exception as exc:
        _runs[run_id] = {"status": "failed", "error": str(exc)}
        logger.error(json.dumps({"event": "drift_run_failed", "run_id": run_id, "error": str(exc)}))


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


@app.post("/runs", response_model=RunResponse)
async def create_run(background_tasks: BackgroundTasks) -> RunResponse:
    """Start a probe + detection run in the background; return a run ID."""
    run_id = str(uuid.uuid4())
    _runs[run_id] = {"status": "running"}
    background_tasks.add_task(_run_monitor_task, run_id)
    return RunResponse(run_id=run_id, status="running")


@app.get("/runs", response_model=list[ProbeRunResponse])
def list_runs(limit: int = 100) -> list[ProbeRunResponse]:
    """List recent probe runs."""
    storage = _get_storage()
    runs = storage.get_all_runs(limit=limit)
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
    storage = _get_storage()
    alerts = storage.get_all_alerts(limit=limit)
    return [AlertResponse(**alert) for alert in alerts]


@app.get("/drift/latest", response_model=DriftLatestResponse)
def get_latest_drift() -> DriftLatestResponse:
    """Return the current drift status and flagged tests."""
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
    storage = _get_storage()
    return storage.get_all_test_results(limit=limit)


@app.get("/health")
def health() -> Dict[str, Any]:
    """Check DB and (optionally) provider health."""
    db_ok = True
    try:
        storage = _get_storage()
        storage.count_successful_runs()
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "unreachable",
        "provider": _get_monitor().adapter.model_id,
    }


# ---------- dashboard UI (served from the same origin; API stays untouched) ----------

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

if os.path.isdir(_WEB_DIR):
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(_WEB_DIR, "index.html"))
