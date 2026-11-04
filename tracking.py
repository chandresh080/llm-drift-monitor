"""MLflow tracking utilities for drift monitoring runs and experiments."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from typing import Any, Dict, Optional, List, Union

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TRACKING_URI = f"sqlite:///{os.path.join(PROJECT_ROOT, 'mlflow.db')}"
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_TRACKING_URI)


def get_tracking_uri() -> str:
    """Return configured MLflow tracking URI."""
    return MLFLOW_TRACKING_URI


def log_drift_run_detached(report: Dict[str, Any]) -> None:
    """Log a drift run in a short lived subprocess and return immediately.

    The report is piped to `python -m tracking` as JSON. mlflow is imported
    only inside that process, so the serving process never pays its memory
    cost or its import time. Failures are logged, never raised: tracking is
    provenance, not a dependency of the pipeline.
    """
    payload = json.dumps(report).encode("utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "tracking"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=PROJECT_ROOT,
    )
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    # Reap asynchronously so a failed import does not leave a zombie.
    def _wait():
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        if proc.wait() != 0 and err:
            print(f"MLflow subprocess failed: {err.strip()[-500:]}", flush=True)
    import threading
    threading.Thread(target=_wait, daemon=True).start()


def get_git_hash() -> str:
    """Return the current git commit hash (HEAD) for reproducibility tracking."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL
        )
        return out.strip()[:8]
    except Exception:
        return "unknown"


@contextmanager
def drift_run(
    run_name: str,
    experiment_name: str = "llm-drift-daily",
    tags: Optional[Dict[str, str]] = None
):
    """
    Context manager that opens an MLflow run under a designated experiment.
    
    Args:
        run_name: Descriptive name for the run.
        experiment_name: MLflow experiment category ('llm-drift-daily' or 'llm-drift-experiments').
        tags: Optional run tags (e.g. {'run_type': 'daily_probe'}).
    """
    import mlflow  # Lazy: mlflow is heavy (RAM); keep service startup light.
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name=run_name, tags=tags) as run:
        yield run


def log_drift_run(
    run,
    params: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    artifacts: Optional[Dict[str, Any]] = None,
    artifact_files: Optional[Union[List[str], Dict[str, str]]] = None
) -> None:
    """
    Log parameters, metrics, dictionary artifacts, and file paths to an active MLflow run.
    """
    if run is None:
        return

    import mlflow  # Lazy: mlflow is heavy (RAM); keep service startup light.

    # Log parameters
    if params:
        for k, v in params.items():
            if v is not None:
                mlflow.log_param(k, str(v))

    # Log metrics
    if metrics:
        for k, v in metrics.items():
            if v is not None:
                try:
                    val = float(v)
                    if not (val != val):  # Skip float('nan')
                        mlflow.log_metric(k, val)
                except (ValueError, TypeError):
                    pass

    # Log dictionary artifacts as JSON
    if artifacts:
        for name, content in artifacts.items():
            try:
                mlflow.log_dict(content, artifact_file=f"reports/{name}.json")
            except Exception:
                pass

    # Log file artifacts (plots, CSVs)
    if artifact_files:
        if isinstance(artifact_files, list):
            for file_path in artifact_files:
                if file_path and os.path.exists(file_path):
                    mlflow.log_artifact(file_path)
        elif isinstance(artifact_files, dict):
            for file_path, artifact_dir in artifact_files.items():
                if file_path and os.path.exists(file_path):
                    mlflow.log_artifact(file_path, artifact_path=artifact_dir)


def _main() -> None:
    """Subprocess entrypoint: read a run report as JSON from stdin and log it."""
    report = json.loads(sys.stdin.read())
    with drift_run(
        run_name=report["run_name"],
        experiment_name=report.get("experiment_name", "llm-drift-daily"),
        tags=report.get("tags"),
    ) as run:
        log_drift_run(
            run,
            params=report.get("params"),
            metrics=report.get("metrics"),
            artifacts=report.get("artifacts"),
        )


if __name__ == "__main__":
    _main()
