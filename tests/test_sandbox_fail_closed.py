"""F-02: sandbox must fail closed, never fail open onto the host shell.

Supplementary EGM blocker (summary-grade). Programme acceptance:
"test with sandbox backend forced absent — command does not execute
on the host shell; approval path is exercised instead" — here the
approval path is exercised by calling with ``approved=True`` (the
post-approval execution step), and the assertion is that even an
*approved* EXECUTE-risk call refuses when the worker layer cannot
guarantee isolation, instead of falling back to the in-process
``execute_fn`` (which for ``execute_shell`` is a host ``shell=True``
subprocess).

Paths pinned:

a. ``WorkerJobObject`` construction fails (e.g. pywin32 / sandbox
   backend absent) -> ``execute_tool_in_worker`` raises ``RuntimeError``,
   spawns NO subprocess, and audits ``WORKER_EXECUTION_REFUSED``.
b. ``read_token`` raises ``FileNotFoundError`` (engine token
   unavailable) -> same refusal, no spawn, same audit event.
c. Registry level: ``ToolRegistry.execute`` for ``execute_shell``
   with the worker layer forced unavailable raises (wrapped as
   ``ToolExecutionError``), the tool's in-process ``execute_fn`` is
   NEVER called, no subprocess is spawned, and a command that would
   have created a file on the host shell leaves no file behind.

Style follows tests/test_phase10_2_worker_auth.py and
tests/test_phase10_2_worker_timeout.py: forced failures are
monkeypatched explicitly so the tests are platform-independent
(they do not rely on pywin32 merely being absent on Linux).
"""
import asyncio
import json

import pytest

from aether_engine.audit import AuditLogger
from aether_engine.tools.registry import (
    ToolExecutionError,
    ToolRegistry,
    create_file_tools,
)
from aether_engine.workers import sandbox
from aether_engine.workers.sandbox import execute_tool_in_worker


def _audit_events(tmp_path):
    log = tmp_path / "logs" / "audit.log"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _refusals(tmp_path):
    return [
        e for e in _audit_events(tmp_path)
        if e.get("action") == "WORKER_EXECUTION_REFUSED"
    ]


class _FakeJobObject:
    """Job Object stand-in whose construction + assignment succeed."""

    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


def _install_spawn_recorder(monkeypatch):
    """Replace asyncio.create_subprocess_exec (as seen by sandbox.py)
    with a recorder that must never be called on a refused run."""
    calls = []

    async def _recorder(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("create_subprocess_exec must not be called on a refused run")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recorder)
    return calls


class TestWorkerJobObjectUnavailable:
    def test_job_object_failure_refuses_without_spawning(self, tmp_path, monkeypatch):
        def _raising_job_object(*args, **kwargs):
            raise RuntimeError("simulated sandbox backend absent (pywin32 unavailable)")

        monkeypatch.setattr(sandbox, "WorkerJobObject", _raising_job_object)
        spawn_calls = _install_spawn_recorder(monkeypatch)
        audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

        with pytest.raises(RuntimeError, match="Refusing to execute"):
            asyncio.run(execute_tool_in_worker(
                tool_name="execute_shell",
                args={"command": "echo PWNED"},
                task_class="standard",
                audit_logger=audit,
            ))

        assert spawn_calls == [], "refused run must spawn no worker subprocess"
        refused = _refusals(tmp_path)
        assert refused, f"expected WORKER_EXECUTION_REFUSED, got {_audit_events(tmp_path)}"
        assert refused[0]["details"]["tool"] == "execute_shell"
        assert "resource limits unenforceable" in refused[0]["details"]["reason"]
        assert not any(
            e.get("action") == "WORKER_EXECUTION_COMPLETED"
            for e in _audit_events(tmp_path)
        )


class TestWorkerTokenUnavailable:
    def test_missing_token_refuses_without_spawning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox, "WorkerJobObject", _FakeJobObject)

        def _missing_token(*args, **kwargs):
            raise FileNotFoundError("Token file not found: .run/engine.token")

        monkeypatch.setattr(sandbox, "read_token", _missing_token)
        spawn_calls = _install_spawn_recorder(monkeypatch)
        audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

        with pytest.raises(RuntimeError, match="Refusing to execute"):
            asyncio.run(execute_tool_in_worker(
                tool_name="execute_shell",
                args={"command": "echo PWNED"},
                task_class="standard",
                audit_logger=audit,
            ))

        assert spawn_calls == [], "refused run must spawn no worker subprocess"
        refused = _refusals(tmp_path)
        assert refused, f"expected WORKER_EXECUTION_REFUSED, got {_audit_events(tmp_path)}"
        assert refused[0]["details"]["tool"] == "execute_shell"
        assert "engine token unavailable" in refused[0]["details"]["reason"]
        assert not any(
            e.get("action") == "WORKER_EXECUTION_COMPLETED"
            for e in _audit_events(tmp_path)
        )


class TestRegistryExecuteShellWorkerUnavailable:
    def test_registry_raises_and_execute_fn_never_runs(self, tmp_path, monkeypatch):
        # Force the worker layer unavailable at its first step.
        def _raising_job_object(*args, **kwargs):
            raise RuntimeError("simulated sandbox backend absent (pywin32 unavailable)")

        monkeypatch.setattr(sandbox, "WorkerJobObject", _raising_job_object)
        spawn_calls = _install_spawn_recorder(monkeypatch)

        # Register the real execute_shell tool, with its genuine
        # host-shell execute_fn wrapped by a spy that calls through:
        # if the registry ever fell back to execute_fn, the spy would
        # record it AND the shell command would really create the file.
        target = tmp_path / "pwned.txt"
        command = f"echo PWNED > {target}"
        registry = ToolRegistry()
        execute_fn_calls = []
        for tool in create_file_tools():
            if tool.name == "execute_shell":
                original_fn = tool.execute_fn

                def _spying_execute_fn(command, _orig=original_fn):
                    execute_fn_calls.append(command)
                    return _orig(command)

                tool.execute_fn = _spying_execute_fn
            registry.register(tool)

        audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

        with pytest.raises(ToolExecutionError):
            asyncio.run(registry.execute(
                "execute_shell",
                {"command": command},
                approved=True,
                workspace_roots=[str(tmp_path)],
                audit_logger=audit,
            ))

        assert execute_fn_calls == [], (
            "registry must not fall back to the in-process execute_fn "
            "when the worker layer is unavailable"
        )
        assert spawn_calls == [], "refused run must spawn no worker subprocess"
        assert not target.exists(), (
            "host shell must not have executed the command: "
            f"{target} was created"
        )
        refused = _refusals(tmp_path)
        assert refused, f"expected WORKER_EXECUTION_REFUSED, got {_audit_events(tmp_path)}"
