"""S2-M4 + S2-M5 (EGM full review): worker sandbox leaks via argv + env.

M4: ``execute_tool_in_worker`` passed the full tool payload as a
``--payload`` argv element — shell commands / file bodies were visible
to any same-user process via ``ps`` / ``/proc/<pid>/cmdline`` and were
bounded by the OS argv size limit.

M5: the worker was spawned with ``env={**os.environ, ...}`` — every
provider API key in the engine's environment was inherited by the
worker (and, transitively, by any shell command it ran).

Fix contract pinned here:
- The payload travels ONLY on the worker's stdin (read to EOF, same
  JSON shape). ``--payload`` is gone — no argv fallback.
- The child env is built from an explicit allowlist (the MCP stdio
  allowlist pattern plus ``AETHER_WORKER_TOKEN``): provider keys and
  unrelated variables never reach the child, while PATH / PYTHONPATH /
  the token do.
- Existing behaviour is preserved: token check, timeouts, output
  capture, and a real end-to-end execution still returns its result.

Style follows tests/test_phase10_2_worker_auth.py: engine-path tests
run the real worker subprocess with cwd in a tmp dir (isolated
``.run/engine.token``) and a faked Windows Job Object on Linux.
"""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "sandbox-stdin-env-test-token"
SECRET_NAME = "AETHER_TEST_SECRET"
SECRET_VALUE = "sentinel-secret-must-not-leak-7f3a"


class FakeJobObject:
    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


@contextmanager
def _engine_call_env(tmp_path):
    """chdir to tmp_path, write the token file, point PYTHONPATH at the
    repo for the worker child, and fake the Job Object. Restores all."""
    token_dir = tmp_path / ".run"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
    old_cwd = os.getcwd()
    old_pythonpath = os.environ.get("PYTHONPATH")
    try:
        os.chdir(tmp_path)
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        with patch("aether_engine.workers.sandbox.WorkerJobObject", FakeJobObject):
            yield
    finally:
        os.chdir(old_cwd)
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


def _run_sandbox(tmp_path, tool_name, args):
    from aether_engine.workers.sandbox import execute_tool_in_worker

    with _engine_call_env(tmp_path):
        return asyncio.run(execute_tool_in_worker(
            tool_name=tool_name, args=args, task_class="standard",
        ))


# ---------------------------------------------------------------------------
# (1) Payload must not appear in the child's argv
# ---------------------------------------------------------------------------

class TestPayloadNotInArgv:
    def test_recorded_spawn_args_have_no_payload(self, tmp_path, monkeypatch):
        """Harness-level: capture the exact argv/env/input the sandbox
        spawns the worker with. The payload JSON may ONLY be the stdin
        input to communicate(); no argv element may contain it."""
        from aether_engine.workers import sandbox

        recorded = {}

        class FakeProc:
            pid = 4242
            returncode = 0

            async def communicate(self, input=None):
                recorded["input"] = input
                return (json.dumps({"ok": True}).encode(), b"")

        async def fake_spawn(*args, **kwargs):
            recorded["args"] = args
            recorded["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(sandbox, "WorkerJobObject", FakeJobObject)
        monkeypatch.setattr(sandbox, "read_token", lambda: TOKEN)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        sentinel = "argv-sentinel-payload-9c1e"
        result = asyncio.run(sandbox.execute_tool_in_worker(
            tool_name="write_file",
            args={"path": str(tmp_path / "x.txt"), "content": sentinel},
            task_class="standard",
        ))
        assert result == {"ok": True}
        argv = recorded["args"]
        assert "--payload" not in argv
        for element in argv:
            assert sentinel not in str(element), f"payload leaked into argv: {argv!r}"
        # The payload still reaches the worker — via stdin, as JSON.
        stdin_payload = json.loads(recorded["input"].decode("utf-8"))
        assert stdin_payload["tool_name"] == "write_file"
        assert stdin_payload["args"]["content"] == sentinel
        assert recorded["kwargs"].get("stdin") is not None

    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc (Linux)")
    def test_real_worker_cmdline_has_no_payload(self, tmp_path):
        """Real spawn: a shell tool asks the kernel for its parent (the
        worker) cmdline. Pre-fix the whole payload — including this
        command itself — is in there; post-fix it must not be."""
        out = tmp_path / "cmdline.txt"
        command = (
            f"tr '\\0' '\\n' < /proc/$PPID/cmdline > {out}"
        )
        result = _run_sandbox(tmp_path, "execute_shell", {"command": command})
        assert result["returncode"] == 0, result
        cmdline = out.read_text(encoding="utf-8")
        assert "aether_worker" in cmdline  # we really read the worker's argv
        assert "--payload" not in cmdline
        # This string occurs only inside the payload (the shell command).
        assert str(out) not in cmdline


# ---------------------------------------------------------------------------
# (2) Env allowlist: secrets stay out, needed vars get through
# ---------------------------------------------------------------------------

class TestWorkerEnvAllowlist:
    def test_recorded_spawn_env_is_allowlisted(self, tmp_path, monkeypatch):
        from aether_engine.workers import sandbox

        recorded = {}

        class FakeProc:
            pid = 4242
            returncode = 0

            async def communicate(self, input=None):
                return (json.dumps({"ok": True}).encode(), b"")

        async def fake_spawn(*args, **kwargs):
            recorded["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
        monkeypatch.setenv("OPENAI_API_KEY", "fake-provider-key")
        monkeypatch.setattr(sandbox, "WorkerJobObject", FakeJobObject)
        monkeypatch.setattr(sandbox, "read_token", lambda: TOKEN)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        asyncio.run(sandbox.execute_tool_in_worker(
            tool_name="write_file",
            args={"path": str(tmp_path / "x.txt"), "content": "hi"},
            task_class="standard",
        ))
        env = recorded["kwargs"]["env"]
        assert SECRET_NAME not in env
        assert "OPENAI_API_KEY" not in env
        assert SECRET_VALUE not in env.values()
        assert env["AETHER_WORKER_TOKEN"] == TOKEN
        assert env.get("PATH"), "worker + its shell children need PATH"
        # Nothing outside the declared allowlist (+ token) may pass.
        assert set(env) <= set(sandbox.WORKER_ENV_ALLOWLIST) | {"AETHER_WORKER_TOKEN"}

    def test_real_worker_child_does_not_see_secret(self, tmp_path, monkeypatch):
        """Real spawn: plant a sentinel secret in the engine env, run a
        shell tool that dumps its environment, and check the dump."""
        monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
        dump = tmp_path / "child-env.txt"
        result = _run_sandbox(
            tmp_path, "execute_shell", {"command": f"env > {dump}"})
        assert result["returncode"] == 0, result
        child_env = dump.read_text(encoding="utf-8")
        assert SECRET_NAME not in child_env
        assert SECRET_VALUE not in child_env
        assert "GEMINI_API_KEY" not in child_env
        assert "fake-gemini-key" not in child_env
        # Allowlisted vars and the worker token DO reach the child.
        assert any(line.startswith("PATH=") and len(line) > len("PATH=")
                   for line in child_env.splitlines())
        assert f"AETHER_WORKER_TOKEN={TOKEN}" in child_env
        assert "PYTHONPATH=" in child_env  # needed for `python -m aether_worker`


# ---------------------------------------------------------------------------
# Direct worker invocation: stdin is the only payload channel
# ---------------------------------------------------------------------------

def _run_worker_direct(tmp_path, stdin_text, extra_argv=(), token_env=TOKEN):
    token_dir = tmp_path / ".run"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AETHER_WORKER_TOKEN"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if token_env is not None:
        env["AETHER_WORKER_TOKEN"] = token_env
    return subprocess.run(
        [sys.executable, "-m", "aether_worker", *extra_argv],
        cwd=tmp_path, env=env, input=stdin_text, check=False,
        capture_output=True, text=True, timeout=30,
    )


class TestWorkerStdinContract:
    def test_payload_on_stdin_executes(self, tmp_path):
        target = tmp_path / "stdin-wrote.txt"
        payload = json.dumps({"tool_name": "write_file",
                              "args": {"path": str(target), "content": "via-stdin"}})
        result = _run_worker_direct(tmp_path, payload)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert target.read_text(encoding="utf-8") == "via-stdin"

    def test_payload_argv_is_rejected_and_executes_nothing(self, tmp_path):
        target = tmp_path / "argv-wrote.txt"
        payload = json.dumps({"tool_name": "write_file",
                              "args": {"path": str(target), "content": "via-argv"}})
        result = _run_worker_direct(tmp_path, "", extra_argv=("--payload", payload))
        assert result.returncode != 0
        assert not target.exists(), "--payload argv fallback must not exist"

    def test_empty_stdin_is_refused(self, tmp_path):
        result = _run_worker_direct(tmp_path, "")
        assert result.returncode != 0

    def test_stdin_payload_still_requires_token(self, tmp_path):
        target = tmp_path / "no-token.txt"
        payload = json.dumps({"tool_name": "write_file",
                              "args": {"path": str(target), "content": "x"}})
        result = _run_worker_direct(tmp_path, payload, token_env=None)
        assert result.returncode != 0
        assert not target.exists()


# ---------------------------------------------------------------------------
# (3) End-to-end regression: sandbox execution still works
# ---------------------------------------------------------------------------

def test_worker_allowlist_matches_mcp_stdio_allowlist():
    """Drift pin: the worker allowlist deliberately mirrors
    ``MCP_STDIO_ENV_ALLOWLIST`` (same subprocess-boundary reasoning).
    It is defined locally in sandbox.py so the workers package does not
    import the MCP client just for a constant — any change to either
    set must be a deliberate edit to both, enforced here."""
    from aether_engine.mcp.client import MCP_STDIO_ENV_ALLOWLIST
    from aether_engine.workers.sandbox import WORKER_ENV_ALLOWLIST

    assert WORKER_ENV_ALLOWLIST == MCP_STDIO_ENV_ALLOWLIST
    for name in WORKER_ENV_ALLOWLIST:
        upper = name.upper()
        assert "KEY" not in upper and "TOKEN" not in upper
        assert "SECRET" not in upper and "PASSWORD" not in upper


class TestSandboxEndToEnd:
    def test_write_and_shell_roundtrip(self, tmp_path):
        target = tmp_path / "e2e.txt"
        result = _run_sandbox(
            tmp_path, "write_file", {"path": str(target), "content": "e2e-ok"})
        assert result == {"path": str(target), "written": True}
        assert target.read_text(encoding="utf-8") == "e2e-ok"

        result = _run_sandbox(
            tmp_path, "execute_shell", {"command": f"cat {target}"})
        assert result["returncode"] == 0
        assert result["stdout"].strip() == "e2e-ok"
