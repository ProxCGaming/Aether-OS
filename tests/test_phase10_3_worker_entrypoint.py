"""Phase 10.3, D2: direct tests for the ``aether_worker`` entry point.

Phase 10.2's ``test_phase10_2_worker_auth.py`` covers the token
refusal (no token / wrong token / unknown tool with valid token), the
valid-token write_file success path, and the engine sandbox call path.
What was untested is the rest of the entry point: malformed payloads,
the missing-token-file refusal, the other allowed tools
(``execute_shell``, ``delete_file``) through the real subprocess, the
``run_worker`` dispatch unit, and the audit-record shape the worker
writes itself (it deliberately does NOT import ``AuditLogger``).

The 10.2 refusal behaviour is re-verified here with one regression
test so a future change that re-opens the backdoor fails loudly.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aether_worker.__main__ import (
    _audit_event,
    _refuse,
    run_worker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "phase10-3-test-token"


# ---------------------------------------------------------------------------
# run_worker dispatch (unit level, no subprocess, no token)
# ---------------------------------------------------------------------------

class TestRunWorker:
    def test_execute_shell_returns_returncode_and_streams(self):
        result = asyncio.run(run_worker({
            "tool_name": "execute_shell",
            "args": {"command": "echo hello-worker"},
        }))
        assert result["returncode"] == 0
        assert result["stdout"].strip() == "hello-worker"
        assert result["stderr"] == ""

    def test_execute_shell_reports_nonzero_returncode(self):
        result = asyncio.run(run_worker({
            "tool_name": "execute_shell",
            "args": {"command": "exit 3"},
        }))
        assert result["returncode"] == 3

    def test_execute_shell_without_command_raises(self):
        with pytest.raises(ValueError, match="Shell command is required"):
            asyncio.run(run_worker({"tool_name": "execute_shell", "args": {}}))

    def test_write_file_writes_content(self, tmp_path):
        target = tmp_path / "w.txt"
        result = asyncio.run(run_worker({
            "tool_name": "write_file",
            "args": {"path": str(target), "content": "via-run-worker"},
        }))
        assert result == {"path": str(target), "written": True}
        assert target.read_text(encoding="utf-8") == "via-run-worker"

    def test_write_file_without_path_raises(self):
        with pytest.raises(ValueError, match="File path is required"):
            asyncio.run(run_worker({"tool_name": "write_file", "args": {}}))

    def test_delete_file_removes_existing_file(self, tmp_path):
        target = tmp_path / "d.txt"
        target.write_text("x", encoding="utf-8")
        result = asyncio.run(run_worker({
            "tool_name": "delete_file", "args": {"path": str(target)},
        }))
        assert result == {"path": str(target), "deleted": True}
        assert not target.exists()

    def test_delete_file_on_missing_file_still_succeeds(self, tmp_path):
        result = asyncio.run(run_worker({
            "tool_name": "delete_file",
            "args": {"path": str(tmp_path / "not-there.txt")},
        }))
        assert result["deleted"] is True

    def test_delete_file_without_path_raises(self):
        with pytest.raises(ValueError, match="File path is required"):
            asyncio.run(run_worker({"tool_name": "delete_file", "args": {}}))

    def test_unknown_tool_raises_instead_of_noop(self):
        with pytest.raises(ValueError, match="Unknown tool: 'bogus_tool'"):
            asyncio.run(run_worker({"tool_name": "bogus_tool", "args": {}}))

    def test_missing_tool_name_raises(self):
        with pytest.raises(ValueError, match="Unknown tool: None"):
            asyncio.run(run_worker({"args": {}}))


# ---------------------------------------------------------------------------
# _audit_event / _refuse helpers (unit level)
# ---------------------------------------------------------------------------

class TestWorkerAuditHelpers:
    def test_audit_event_writes_audit_logger_log_event_shape(self, tmp_path, monkeypatch):
        # S1-N1: the worker's audit path is home-based now, not
        # cwd-relative — pin the module constant at the tmp log.
        monkeypatch.setattr(
            "aether_worker.__main__.DEFAULT_AUDIT_LOG_PATH",
            tmp_path / "logs" / "audit.log")
        _audit_event("WORKER_TEST", {"tool": "write_file"})
        log = tmp_path / "logs" / "audit.log"
        (record,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        # Exact shape AuditLogger.log_event produces (see __main__ docstring).
        assert set(record) == {"type", "ts", "action", "details"}
        assert record["type"] == "ENGINE_EVENT"
        assert record["action"] == "WORKER_TEST"
        assert record["details"] == {"tool": "write_file"}
        assert isinstance(record["ts"], float)

    def test_audit_event_shape_matches_audit_logger(self, tmp_path, monkeypatch):
        """The worker hand-rolls the record; it must stay identical to
        what AuditLogger.log_event writes, or log consumers diverge."""
        from aether_engine.audit import AuditLogger

        monkeypatch.setattr(
            "aether_worker.__main__.DEFAULT_AUDIT_LOG_PATH",
            tmp_path / "logs" / "audit.log")
        _audit_event("SHAPE_CHECK", {"tool": "x"})
        AuditLogger(log_path=tmp_path / "audit2.log").log_event("SHAPE_CHECK", {"tool": "x"})
        worker_record = json.loads((tmp_path / "logs" / "audit.log").read_text(encoding="utf-8"))
        logger_record = json.loads((tmp_path / "audit2.log").read_text(encoding="utf-8"))
        assert set(worker_record) == set(logger_record)

    def test_refuse_returns_1_writes_audit_event_and_reports_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "aether_worker.__main__.DEFAULT_AUDIT_LOG_PATH",
            tmp_path / "logs" / "audit.log")
        code = _refuse("WORKER_AUTH_REFUSED", "write_file", "missing_token")
        assert code == 1
        (record,) = [json.loads(line) for line in
                     (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8").splitlines()]
        assert record["action"] == "WORKER_AUTH_REFUSED"
        assert record["details"] == {"tool": "write_file", "reason": "missing_token"}
        err = json.loads(capsys.readouterr().err)
        assert err == {"status": "refused", "reason": "missing_token"}


# ---------------------------------------------------------------------------
# main() through the real subprocess (token + payload handling)
# ---------------------------------------------------------------------------

def _run_worker_subprocess(tmp_path, payload, token_env=None, token_file=None):
    if token_file is not None:
        token_dir = tmp_path / ".run"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "engine.token").write_text(token_file, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AETHER_WORKER_TOKEN"}
    # S1-N1: the worker's audit log lives under ~/.aether — pin the
    # child's home to tmp_path so the trail stays isolated per test.
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if token_env is not None:
        env["AETHER_WORKER_TOKEN"] = token_env
    return subprocess.run(
        # S2-M4: payload travels on stdin, never argv.
        [sys.executable, "-m", "aether_worker"],
        cwd=tmp_path, env=env, input=payload,
        capture_output=True, text=True, timeout=30, check=False,
    )


def _audit_events(tmp_path):
    log = tmp_path / ".aether" / "logs" / "audit.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestMainSubprocess:
    def test_malformed_json_payload_is_refused_without_executing(self, tmp_path):
        target = tmp_path / "pwned.txt"
        result = _run_worker_subprocess(
            tmp_path, "{not json", token_env=TOKEN, token_file=TOKEN)
        assert result.returncode != 0
        assert "Invalid payload JSON" in result.stderr
        assert not target.exists()

    def test_non_dict_payload_is_refused(self, tmp_path):
        result = _run_worker_subprocess(
            tmp_path, '["write_file"]', token_env=TOKEN, token_file=TOKEN)
        assert result.returncode != 0
        assert "expected a JSON object" in result.stderr

    def test_missing_token_file_is_refused_even_with_token_env(self, tmp_path):
        target = tmp_path / "pwned.txt"
        payload = json.dumps({"tool_name": "write_file",
                              "args": {"path": str(target), "content": "x"}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN)
        assert result.returncode != 0
        assert not target.exists()
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_AUTH_REFUSED" for e in events)
        assert any("token_file_unavailable" in str(e.get("details", {}))
                   for e in events if e.get("action") == "WORKER_AUTH_REFUSED")
        # The refusal must never leak the token value.
        assert TOKEN not in (tmp_path / ".aether" / "logs" / "audit.log").read_text(encoding="utf-8")

    def test_valid_token_execute_shell_via_subprocess(self, tmp_path):
        target = tmp_path / "shelled.txt"
        payload = json.dumps({"tool_name": "execute_shell",
                              "args": {"command": f"touch {target}"}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert target.exists()
        assert json.loads(result.stdout)["returncode"] == 0
        assert not any(e.get("action") == "WORKER_AUTH_REFUSED"
                       for e in _audit_events(tmp_path))

    def test_valid_token_delete_file_via_subprocess(self, tmp_path):
        target = tmp_path / "gone.txt"
        target.write_text("x", encoding="utf-8")
        payload = json.dumps({"tool_name": "delete_file", "args": {"path": str(target)}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert not target.exists()
        assert json.loads(result.stdout) == {"path": str(target), "deleted": True}

    def test_unknown_tool_with_valid_token_is_execution_refused(self, tmp_path):
        payload = json.dumps({"tool_name": "bogus_tool", "args": {}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        assert result.returncode != 0
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_EXECUTION_REFUSED" for e in events)
        assert json.loads(result.stderr)["status"] == "refused"


class TestPhase102RefusalStillHolds:
    """Regression re-verification of the Phase 10.2 fix: direct
    invocation with no token and no token file must still be refused
    and must execute nothing."""

    def test_no_token_no_token_file_is_still_refused(self, tmp_path):
        target = tmp_path / "pwned.txt"
        payload = json.dumps({"tool_name": "write_file",
                              "args": {"path": str(target), "content": "backdoor"}})
        result = _run_worker_subprocess(tmp_path, payload)
        assert result.returncode != 0
        assert not target.exists()
        events = _audit_events(tmp_path)
        assert any(e.get("action") == "WORKER_AUTH_REFUSED" for e in events)
        assert "backdoor" not in (tmp_path / ".aether" / "logs" / "audit.log").read_text(encoding="utf-8")
