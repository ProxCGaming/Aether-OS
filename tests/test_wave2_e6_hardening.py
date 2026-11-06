"""Wave 2 E6 hardening regressions.

These tests pin four previously silent failure modes:

* a corrupt plugin registry must be surfaced and preserved, never
  silently replaced by an empty registry on the next save;
* an MCP disconnect timeout must deregister the abandoned session;
* a skill's explicitly declared ``injection.max_tokens`` cap must
  bound the content returned for injection;
* cancelling or timing out a worker call must stop the subprocess,
  escalating from terminate to kill when terminate is ignored.
"""

import asyncio
import json

import pytest

from aether_engine.mcp.client import MCPClientManager
from aether_engine.plugins.installer import PluginInstaller
from aether_engine.skills.manager import SkillManager
from aether_engine.workers import sandbox

# ---------------------------------------------------------------------------
# E6-NEW-01: corrupt plugin registry
# ---------------------------------------------------------------------------


def test_corrupt_plugin_registry_is_preserved_and_save_is_refused(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    registry_file = plugins_dir / "registry.json"
    original = b'{"owned-plugin": {"skill_claims": '
    registry_file.write_bytes(original)

    installer = PluginInstaller(plugins_dir)

    assert getattr(installer, "registry_load_error", None)
    backup = registry_file.with_name(registry_file.name + ".bak")
    assert backup.exists()
    assert backup.read_bytes() == original
    assert registry_file.read_bytes() == original

    with pytest.raises((ValueError, RuntimeError), match="corrupt"):
        installer._save_registry()
    assert registry_file.read_bytes() == original


def test_valid_plugin_registry_still_round_trips(tmp_path):
    plugins_dir = tmp_path / "plugins"
    installer = PluginInstaller(plugins_dir)
    installer._registry = {"demo": {"enabled": True}}
    installer._save_registry()

    fresh = PluginInstaller(plugins_dir)
    assert getattr(fresh, "registry_load_error", None) is None
    assert fresh._registry == {"demo": {"enabled": True}}


# ---------------------------------------------------------------------------
# E6-NEW-03: MCP disconnect timeout
# ---------------------------------------------------------------------------


async def test_disconnect_timeout_force_deregisters_session(monkeypatch):
    manager = MCPClientManager()
    manager._sessions["fake"] = object()
    manager._stacks["fake"] = object()
    manager._configs["fake"] = {"transport": "streamable_http"}
    manager._set_status("fake", "connected", tool_count=1)
    close_event = asyncio.Event()
    manager._close_events["fake"] = close_event
    owner = asyncio.create_task(asyncio.sleep(60))
    manager._owner_tasks["fake"] = owner

    real_wait_for = asyncio.wait_for

    async def timeout_wait_for(awaitable, timeout):
        if timeout == 30:
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", timeout_wait_for)
    try:
        result = await manager.disconnect("fake")
    finally:
        monkeypatch.undo()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

    assert result == {"ok": True, "error": None}
    assert close_event.is_set()
    assert manager.is_connected("fake") is False
    assert "fake" not in manager._sessions
    assert "fake" not in manager._stacks
    assert "fake" not in manager._configs
    assert manager.get_status("fake")["status"] == "disconnected"


# ---------------------------------------------------------------------------
# E6-NEW-06: skill injection token cap
# ---------------------------------------------------------------------------


def _skill_manager(tmp_path, *, body, injection=None):
    root = tmp_path / "skills"
    skill_dir = root / "capped-skill"
    skill_dir.mkdir(parents=True)
    manifest = {"name": "capped-skill", "description": "cap probe"}
    if injection is not None:
        manifest["injection"] = injection
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return SkillManager(global_dir=tmp_path / "global", workspace_dirs=[root])


def test_explicit_skill_max_tokens_bounds_loaded_content(tmp_path):
    body = " ".join(f"word{i}" for i in range(100))
    manager = _skill_manager(tmp_path, body=body, injection={"max_tokens": 5})

    content = manager.load_skill_content("capped-skill")

    assert content != body
    # The manager's budgeting convention is the codebase's approximate
    # four-chars-per-token estimate: five tokens therefore allow at
    # most twenty characters of injected body.
    assert len(content) <= 20
    assert body.startswith(content)


def test_skill_without_explicit_max_tokens_keeps_existing_behaviour(tmp_path):
    body = "x" * 9001
    manager = _skill_manager(tmp_path, body=body)

    assert manager.load_skill_content("capped-skill") == body


# ---------------------------------------------------------------------------
# E6-NEW-11 / N-30: worker subprocess cleanup
# ---------------------------------------------------------------------------


class _FakeJobObject:
    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


class _StubbornProc:
    pid = 4242

    def __init__(self):
        self.returncode = None
        self.communicate_started = asyncio.Event()
        self._release = asyncio.Event()
        self._done = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def communicate(self, input=None):
        self.communicate_started.set()
        await self._release.wait()
        return (b"{}", b"")

    def terminate(self):
        self.terminate_calls += 1
        # Deliberately ignore termination: cleanup must escalate.

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._release.set()
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


def _patch_worker(monkeypatch, proc):
    async def fake_spawn(*args, **kwargs):
        return proc

    monkeypatch.setattr(sandbox, "WorkerJobObject", _FakeJobObject)
    monkeypatch.setattr(sandbox, "read_token", lambda: "test-token")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(
        sandbox, "WORKER_TERMINATE_GRACE_SECONDS", 0.01, raising=False
    )
    monkeypatch.setattr(sandbox, "WORKER_KILL_GRACE_SECONDS", 0.01, raising=False)


async def test_cancelled_worker_call_terminates_then_kills_subprocess(monkeypatch):
    proc = _StubbornProc()
    _patch_worker(monkeypatch, proc)

    task = asyncio.create_task(
        sandbox.execute_tool_in_worker(
            tool_name="execute_shell",
            args={"command": "sleep 60"},
            task_class="standard",
        )
    )
    await proc.communicate_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.returncode == -9


async def test_timed_out_worker_call_terminates_then_kills_subprocess(monkeypatch):
    proc = _StubbornProc()
    _patch_worker(monkeypatch, proc)

    with pytest.raises(RuntimeError, match="timed out"):
        await sandbox.execute_tool_in_worker(
            tool_name="execute_shell",
            args={"command": "sleep 60"},
            task_class="instant",
            task_classes={
                "instant": {"time_cap_seconds": 0.05, "memory_cap_mb": 512}
            },
        )

    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    assert proc.returncode == -9
