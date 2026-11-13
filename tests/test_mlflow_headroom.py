"""Memory headroom guard for the detached MLflow logging subprocess.

The mlflow import costs roughly 400 MB inside the child process and counts
against the instance ceiling even though it is not resident in the serving
process: on the hosted free tier that child was the OOM killer's trigger at
cycle end. The guard skips tracking when the container cannot afford it.
"""
import pytest

from monitor import _memory_headroom_mb, _mlflow_headroom_ok


def _write_cgroup(base, limit, current):
    base.mkdir(parents=True, exist_ok=True)
    (base / "memory.max").write_text(limit + "\n", encoding="utf-8")
    (base / "memory.current").write_text(current + "\n", encoding="utf-8")


def test_headroom_from_cgroup_v2_files(tmp_path):
    _write_cgroup(tmp_path, "536870912", "134217728")  # 512 MB limit, 128 MB used
    assert _memory_headroom_mb(base=str(tmp_path)) == pytest.approx(384.0)


def test_headroom_unknown_without_cgroup_files(tmp_path):
    assert _memory_headroom_mb(base=str(tmp_path / "absent")) is None


def test_headroom_unknown_when_uncapped(tmp_path):
    _write_cgroup(tmp_path, "max", "134217728")
    assert _memory_headroom_mb(base=str(tmp_path)) is None


def test_headroom_unknown_with_garbage_values(tmp_path):
    _write_cgroup(tmp_path, "five", "twelve")
    assert _memory_headroom_mb(base=str(tmp_path)) is None


def test_guard_allows_generous_headroom():
    assert _mlflow_headroom_ok(1000.0)


def test_guard_allows_unknown_headroom():
    """Hosts without cgroup accounting keep tracking (previous behavior)."""
    assert _mlflow_headroom_ok(None)


def test_guard_rejects_tight_headroom():
    """Below the mlflow import cost the child would OOM the instance."""
    assert not _mlflow_headroom_ok(384.0)
