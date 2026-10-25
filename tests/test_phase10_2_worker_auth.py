"""Phase 10.2, A1: the worker is an unauthenticated backdoor — fixed.

Before the fix, ``python -m aether_worker --payload ...`` dispatched any
tool with no token, no policy, no workspace check (``read_token`` was
imported but never called). These tests fail on the pre-fix code and
pass after: direct invocation without/with a wrong token is refused,
nothing executes, and a distinct audit event is recorded; the engine's
own call path (sandbox -> worker, token passed via env) still works.

Each direct-invocation test runs the worker as a real subprocess with
cwd in a tmp dir, so its ``.run/engine.token`` and ``logs/audit.log``
are fully isolated from the repo.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "phase10-2-test-token"

def _write_payload(target):
    return json.dumps({
        "tool_name": "write_file",
        "args": {"path": str(target), "content": "backdoor"},
    })


def _shell_payload(target):
    return json.dumps({
        "tool_name": "execute_shell",
        "args": {"command": f"touch {target}"},
    })


def _run_worker(tmp_path, payload, token_env=None, token_file=None):
    """Run the worker as a subprocess with cwd=tmp_path.

    token_env: value for AETHER_WORKER_TOKEN (None = unset).
    token_file: token to write to .run/engine.token (None = no file).
    """
    if token_file is not None:
        token_dir = tmp_path / ".run"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "engine.token").write_text(token_file, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AETHER_WORKER_TOKEN"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if token_env is not None:
        env["AETHER_WORKER_TOKEN"] = token_env
    return subprocess.run(
        [sys.executable, "-m", "aether_worker", "--payload", payload],
        cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )


def _audit_events(tmp_path):
    log = tmp_path / "logs" / "audit.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_refused(result, target, tmp_path, action="WORKER_AUTH_REFUSED"):
    assert result.returncode != 0, f"worker must refuse, got stdout={result.stdout!r}"
    assert not target.exists(), "refused worker invocation must not execute anything"
    events = _audit_events(tmp_path)
    assert any(e.get("action") == action for e in events), (
        f"expected audit event {action}, got {events}"
    )
    # The refusal must never leak the token or the tool arguments.
    assert TOKEN not in (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8")
    assert "backdoor" not in (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8")


class TestDirectWorkerInvocationRefused:
    def test_no_token_no_token_file_is_refused(self, tmp_path):
        target = tmp_path / "pwned.txt"
        result = _run_worker(tmp_path, _write_payload(target))
        _assert_refused(result, target, tmp_path)

    def test_wrong_token_is_refused(self, tmp_path):
        target = tmp_path / "shelled.txt"
        result = _run_worker(
            tmp_path, _shell_payload(target),
            token_env="wrong-token", token_file=TOKEN,
        )
        _assert_refused(result, target, tmp_path)

    def test_unknown_tool_refused_even_with_valid_token(self, tmp_path):
        payload = json.dumps({"tool_name": "bogus_tool", "args": {}})
        result = _run_worker(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        assert result.returncode != 0
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_EXECUTION_REFUSED" for e in events), events


class TestDirectWorkerInvocationAuthenticated:
    def test_valid_token_executes(self, tmp_path):
        target = tmp_path / "ok.txt"
        result = _run_worker(
            tmp_path, _write_payload(target),
            token_env=TOKEN, token_file=TOKEN,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert target.read_text(encoding="utf-8") == "backdoor"
        events = _audit_events(tmp_path)
        assert not any(e.get("action") == "WORKER_AUTH_REFUSED" for e in events), events


class TestEngineWorkerCallPath:
    def test_engine_sandbox_call_still_works(self, tmp_path):
        """The engine's own path: sandbox reads the token, passes it to
        the worker via env, worker authenticates and executes.

        On Linux the Windows Job Object cannot attach, so the job object
        is faked (assign_process no-op) — the token handoff and the real
        worker subprocess are exercised, not mocked.
        """
        from aether_engine.audit import AuditLogger
        from aether_engine.workers.sandbox import execute_tool_in_worker

        token_dir = tmp_path / ".run"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
        target = tmp_path / "engine-wrote.txt"
        audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

        class FakeJobObject:
            def __init__(self, *args, **kwargs):
                pass

            def assign_process(self, pid):
                pass

        old_cwd = os.getcwd()
        old_pythonpath = os.environ.get("PYTHONPATH")
        try:
            # Both sandbox.read_token() and the worker child resolve
            # .run/engine.token relative to the process cwd. The chdir
            # also takes the repo off the child's module path, so point
            # PYTHONPATH at it for this call (restored below).
            os.chdir(tmp_path)
            os.environ["PYTHONPATH"] = str(REPO_ROOT)
            with patch("aether_engine.workers.sandbox.WorkerJobObject", FakeJobObject):
                result = asyncio.run(execute_tool_in_worker(
                    tool_name="write_file",
                    args={"path": str(target), "content": "via-engine"},
                    task_class="standard",
                    audit_logger=audit,
                ))
        finally:
            os.chdir(old_cwd)
            if old_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = old_pythonpath

        assert result == {"path": str(target), "written": True}, result
        assert target.read_text(encoding="utf-8") == "via-engine"
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_EXECUTION_COMPLETED" for e in events), events
        assert not any(e.get("action") == "WORKER_AUTH_REFUSED" for e in events), events
