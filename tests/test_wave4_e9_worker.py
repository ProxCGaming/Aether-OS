"""Wave 4 / E9 remainder — aether_worker/__main__.py.

E9-W1: non-ValueError exceptions escaped main()'s refusal path — a
bare traceback, no audit record — contradicting the module docstring
("malformed payloads exit non-zero, execute nothing, and are recorded
in the audit log"). Every malformed shape below must now be refused
AND audited. (Pass B refuted the card's truncation sub-claim:
``Path.write_text`` type-checks before opening, so the content=123
shape never truncated — pinned here as a regression guard.)

E9-W2: execute_shell buffered unbounded output via communicate() and
had no in-worker timeout. Output is now capped per stream (drained,
not blocked) and the call is time-bounded in the worker.

E9-W3: the pre-auth stdin read was unbounded in bytes and could block
forever on a held-open pipe; the comment claimed it "cannot block".
The read is now bounded in both bytes and time, and the refusal is
audited like every other pre-auth refusal.

E9-W4: delete_file reported {"deleted": True} for a path that never
existed. It still succeeds as a no-op, but the flag tells the truth.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import aether_worker.__main__ as worker_mod
from aether_worker.__main__ import run_worker

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "wave4-e9-test-token"


# ---------------------------------------------------------------------------
# subprocess plumbing (mirrors test_phase10_3_worker_entrypoint)
# ---------------------------------------------------------------------------

def _run_worker_subprocess(tmp_path, payload, token_env=None, token_file=None):
    if token_file is not None:
        token_dir = tmp_path / ".run"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "engine.token").write_text(token_file, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AETHER_WORKER_TOKEN"}
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if token_env is not None:
        env["AETHER_WORKER_TOKEN"] = token_env
    return subprocess.run(
        [sys.executable, "-m", "aether_worker"],
        cwd=tmp_path, env=env, input=payload,
        capture_output=True, text=True, timeout=30, check=False,
    )


def _audit_events(tmp_path):
    log = tmp_path / ".aether" / "logs" / "audit.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_refused_and_audited(result, tmp_path):
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stderr)["status"] == "refused"
    events = _audit_events(tmp_path)
    assert any(e.get("action") == "WORKER_EXECUTION_REFUSED" for e in events), events


# ---------------------------------------------------------------------------
# E9-W1 — every malformed shape is refused AND audited
# ---------------------------------------------------------------------------

class TestW1RefusalCoversNonValueErrors:
    def test_args_as_list_is_refused_and_audited(self, tmp_path):
        payload = json.dumps({"tool_name": "write_file", "args": ["not", "a", "dict"]})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)

    def test_tool_name_as_list_is_refused_and_audited(self, tmp_path):
        payload = json.dumps({"tool_name": ["write_file"], "args": {}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)

    def test_write_file_non_string_content_refused_file_untouched(self, tmp_path):
        target = tmp_path / "keep.txt"
        target.write_text("original", encoding="utf-8")
        payload = json.dumps({
            "tool_name": "write_file",
            "args": {"path": str(target), "content": 123},
        })
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)
        # Pass B regression guard: no truncation ever happened.
        assert target.read_text(encoding="utf-8") == "original"

    def test_delete_file_on_directory_is_refused_and_audited(self, tmp_path):
        directory = tmp_path / "a-dir"
        directory.mkdir()
        payload = json.dumps({"tool_name": "delete_file", "args": {"path": str(directory)}})
        result = _run_worker_subprocess(tmp_path, payload, token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)
        assert directory.is_dir()

    def test_invalid_json_is_refused_and_audited(self, tmp_path):
        result = _run_worker_subprocess(tmp_path, "{not json", token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)

    def test_non_dict_payload_is_refused_and_audited(self, tmp_path):
        result = _run_worker_subprocess(tmp_path, '["write_file"]', token_env=TOKEN, token_file=TOKEN)
        _assert_refused_and_audited(result, tmp_path)


# ---------------------------------------------------------------------------
# E9-W2 — execute_shell output cap + in-worker timeout
# ---------------------------------------------------------------------------

class TestW2ShellBounds:
    def test_stdout_is_capped_and_marked(self, monkeypatch):
        monkeypatch.setattr(worker_mod, "SHELL_OUTPUT_MAX_BYTES", 100)
        command = (
            f"{sys.executable} -c \"import sys; sys.stdout.write('a' * 200000)\""
        )
        result = asyncio.run(run_worker({
            "tool_name": "execute_shell", "args": {"command": command},
        }))
        assert result["returncode"] == 0
        assert len(result["stdout"]) < 1000
        assert "truncated" in result["stdout"]

    def test_stderr_beyond_cap_is_drained_not_deadlocked(self, monkeypatch):
        monkeypatch.setattr(worker_mod, "SHELL_OUTPUT_MAX_BYTES", 100)
        # Safety net only: a correct drain finishes in well under this.
        monkeypatch.setattr(worker_mod, "SHELL_TIMEOUT_S", 20)
        command = (
            f"{sys.executable} -c \"import sys; sys.stderr.write('b' * 200000)\""
        )
        result = asyncio.run(run_worker({
            "tool_name": "execute_shell", "args": {"command": command},
        }))
        assert result["returncode"] == 0
        assert not result.get("timed_out")
        assert "truncated" in result["stderr"]

    def test_shell_timeout_is_reported_in_band(self, monkeypatch):
        monkeypatch.setattr(worker_mod, "SHELL_TIMEOUT_S", 0.2)
        start = time.monotonic()
        result = asyncio.run(run_worker({
            "tool_name": "execute_shell", "args": {"command": "sleep 5"},
        }))
        elapsed = time.monotonic() - start
        assert elapsed < 4, "the worker must not wait out the full sleep"
        assert result["timed_out"] is True
        assert result["returncode"] != 0
        assert "timed out" in result["stderr"]


# ---------------------------------------------------------------------------
# E9-W3 — stdin read bounded in bytes and time, refusal audited
# ---------------------------------------------------------------------------

class _FakeStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


class _BlockingStdin:
    """A stdin whose buffer never yields: a live writer holding the
    pipe open without finishing (the E9-W3 shape)."""

    def __init__(self):
        self.release = threading.Event()

        class _Buffer:
            def __init__(self, event):
                self._event = event

            def read(self, _n=-1):
                self._event.wait(timeout=30)
                return b""

        self.buffer = _Buffer(self.release)


class TestW3StdinBounds:
    def test_oversized_payload_is_refused_and_audited(self, tmp_path, monkeypatch):
        audit_log = tmp_path / "logs" / "audit.log"
        monkeypatch.setattr(worker_mod, "DEFAULT_AUDIT_LOG_PATH", audit_log)
        oversized = b"x" * (worker_mod.MAX_PAYLOAD_BYTES + 1)
        monkeypatch.setattr(sys, "stdin", _FakeStdin(oversized))
        code = asyncio.run(worker_mod.main())
        assert code == 1
        (record,) = [json.loads(line) for line in
                     audit_log.read_text(encoding="utf-8").splitlines()]
        assert record["action"] == "WORKER_AUTH_REFUSED"
        assert record["details"]["reason"] == "payload_too_large"

    def test_held_open_stdin_times_out_and_is_audited(self, tmp_path, monkeypatch):
        audit_log = tmp_path / "logs" / "audit.log"
        monkeypatch.setattr(worker_mod, "DEFAULT_AUDIT_LOG_PATH", audit_log)
        monkeypatch.setattr(worker_mod, "PAYLOAD_READ_TIMEOUT_S", 0.2)
        fake = _BlockingStdin()
        monkeypatch.setattr(sys, "stdin", fake)
        start = time.monotonic()
        try:
            code = asyncio.run(worker_mod.main())
        finally:
            fake.release.set()
        elapsed = time.monotonic() - start
        assert code == 1
        assert elapsed < 10, "the read must be deadline-bounded, not indefinite"
        (record,) = [json.loads(line) for line in
                     audit_log.read_text(encoding="utf-8").splitlines()]
        assert record["action"] == "WORKER_AUTH_REFUSED"
        assert record["details"]["reason"] == "payload_read_timeout"


# ---------------------------------------------------------------------------
# E9-W4 — delete_file tells the truth
# ---------------------------------------------------------------------------

class TestW4DeleteHonesty:
    def test_missing_file_reports_deleted_false_but_succeeds(self, tmp_path):
        result = asyncio.run(run_worker({
            "tool_name": "delete_file",
            "args": {"path": str(tmp_path / "not-there.txt")},
        }))
        assert result == {"path": str(tmp_path / "not-there.txt"), "deleted": False}

    def test_existing_file_reports_deleted_true(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("x", encoding="utf-8")
        result = asyncio.run(run_worker({
            "tool_name": "delete_file", "args": {"path": str(target)},
        }))
        assert result == {"path": str(target), "deleted": True}
        assert not target.exists()

    def test_dangling_symlink_delete_reports_true(self, tmp_path):
        link = tmp_path / "link.txt"
        os.symlink(tmp_path / "no-such-target", link)
        result = asyncio.run(run_worker({
            "tool_name": "delete_file", "args": {"path": str(link)},
        }))
        assert result["deleted"] is True
        assert not os.path.lexists(link)
