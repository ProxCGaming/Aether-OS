"""Phase 10.2, A2: no wall-clock timeout on worker execution; assign_process
failure swallowed.

Before the fix, ``execute_tool_in_worker`` awaited ``proc.communicate()``
with no timeout (the Job Object limit is CPU user-time only, so a sleeping
worker is never killed), and ``assign_process`` failure was swallowed with
``except Exception: pass`` at the call site in ``sandbox.py`` — the worker
then ran with no limits at all. (Re-verified: the swallow is in
``aether_engine/workers/sandbox.py``, not in ``job_object.py`` — that
module's ``assign_process`` logs and re-raises; the audit's
``sandbox.py:88-91`` citation points at the call site.)

These tests fail on the pre-fix code and pass after: a worker that sleeps
past its profile cap is killed within the wall-clock bound (distinct
``WORKER_EXECUTION_TIMEOUT`` audit event), and a forced ``assign_process``
failure refuses execution (``WORKER_EXECUTION_REFUSED``, nothing runs).

Each engine-path test runs the real worker subprocess with cwd in a tmp
dir, so its ``.run/engine.token`` and ``logs/audit.log`` are fully
isolated from the repo. On Linux the Windows Job Object cannot attach,
so it is faked (same pattern as tests/test_phase10_2_worker_auth.py).
"""
import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from aether_engine.workers.sandbox import _DEFAULT_TASK_CLASSES

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "phase10-2-test-token"


def _audit_events(tmp_path):
    log = tmp_path / "logs" / "audit.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@contextmanager
def _engine_call_env(tmp_path, job_cls):
    """chdir to tmp_path (token file + audit log resolve relative to cwd),
    point PYTHONPATH at the repo for the worker child, and fake the
    Windows Job Object. Restores everything on exit."""
    from aether_engine.audit import AuditLogger

    token_dir = tmp_path / ".run"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
    audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

    old_cwd = os.getcwd()
    old_pythonpath = os.environ.get("PYTHONPATH")
    try:
        os.chdir(tmp_path)
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        with patch("aether_engine.workers.sandbox.WorkerJobObject", job_cls):
            yield audit
    finally:
        os.chdir(old_cwd)
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


class FakeJobObject:
    """Job Object stand-in: construction + assignment succeed (no-op)."""

    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


class FailingAssignJobObject(FakeJobObject):
    """Job Object stand-in: assignment always fails (race / permissions)."""

    def assign_process(self, pid):
        raise RuntimeError("simulated AssignProcessToJobObject failure")


class TestWallClockTimeout:
    def test_worker_sleeping_past_cap_is_killed_within_bound(self, tmp_path):
        """instant cap overridden to 2s; worker runs `sleep 10`. Pre-fix
        this returns successfully after ~10s (no timeout); post-fix the
        engine kills the worker at ~2s and raises."""
        from aether_engine.workers.sandbox import execute_tool_in_worker

        task_classes = {"instant": {"time_cap_seconds": 2, "memory_cap_mb": 512}}
        with _engine_call_env(tmp_path, FakeJobObject) as audit:
            start = time.monotonic()
            with pytest.raises(RuntimeError, match="timed out"):
                asyncio.run(execute_tool_in_worker(
                    tool_name="execute_shell",
                    args={"command": "sleep 10"},
                    task_class="instant",
                    audit_logger=audit,
                    task_classes=task_classes,
                ))
            elapsed = time.monotonic() - start

        # Killed at the 2s cap, not when the 10s sleep would have ended.
        assert elapsed < 8, f"worker not killed within bound, took {elapsed:.1f}s"
        assert elapsed >= 1.5, f"killed suspiciously early ({elapsed:.1f}s)"
        events = _audit_events(tmp_path)
        timeout_events = [e for e in events if e.get("action") == "WORKER_EXECUTION_TIMEOUT"]
        assert timeout_events, f"expected WORKER_EXECUTION_TIMEOUT event, got {events}"
        assert timeout_events[0]["details"]["time_cap_seconds"] == 2
        assert timeout_events[0]["details"]["task_class"] == "instant"
        assert not any(e.get("action") == "WORKER_EXECUTION_COMPLETED" for e in events)

    def test_fast_worker_completes_within_cap(self, tmp_path):
        """Regression guard: wrapping communicate() in wait_for must not
        break the normal path — a fast worker still completes."""
        from aether_engine.workers.sandbox import execute_tool_in_worker

        target = tmp_path / "fast.txt"
        with _engine_call_env(tmp_path, FakeJobObject) as audit:
            result = asyncio.run(execute_tool_in_worker(
                tool_name="write_file",
                args={"path": str(target), "content": "fast"},
                task_class="instant",
                audit_logger=audit,
            ))
        assert result == {"path": str(target), "written": True}, result
        assert target.read_text(encoding="utf-8") == "fast"
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_EXECUTION_COMPLETED" for e in events), events


class TestAssignProcessFailureRefuses:
    def test_assign_process_failure_refuses_execution(self, tmp_path):
        """Forced assign_process failure must refuse the run (fail closed),
        not log-and-continue unlimited. Pre-fix the worker executed and
        the file was written; post-fix nothing runs."""
        from aether_engine.workers.sandbox import execute_tool_in_worker

        target = tmp_path / "must-not-exist.txt"
        with _engine_call_env(tmp_path, FailingAssignJobObject) as audit:
            with pytest.raises(RuntimeError, match="Refusing to execute"):
                asyncio.run(execute_tool_in_worker(
                    tool_name="write_file",
                    args={"path": str(target), "content": "unlimited"},
                    task_class="standard",
                    audit_logger=audit,
                ))
        assert not target.exists(), "refused execution must not run the tool"
        events = _audit_events(tmp_path)
        refused = [e for e in events if e.get("action") == "WORKER_EXECUTION_REFUSED"]
        assert refused, f"expected WORKER_EXECUTION_REFUSED event, got {events}"
        assert "assign" in refused[0]["details"]["reason"].lower()
        assert not any(e.get("action") == "WORKER_EXECUTION_COMPLETED" for e in events)


class TestProfileTimeCaps:
    def test_default_profile_time_caps_are_the_wall_clock_bounds(self):
        """The wall-clock bound for a call is the time cap of the profile
        actually in use: instant 10s / quick 60s / standard 1800s /
        heavy 7200s (matches the prompt; no better bound in the code —
        Tool.timeout_s=60 exists but is never read anywhere)."""
        assert _DEFAULT_TASK_CLASSES["instant"]["time_cap_seconds"] == 10
        assert _DEFAULT_TASK_CLASSES["quick"]["time_cap_seconds"] == 60
        assert _DEFAULT_TASK_CLASSES["standard"]["time_cap_seconds"] == 1800
        assert _DEFAULT_TASK_CLASSES["heavy"]["time_cap_seconds"] == 7200
