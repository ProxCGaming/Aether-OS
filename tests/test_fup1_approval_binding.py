"""FUP-1 (xref S2-M1): approval strictness adjustable from Settings.

S2-M1 (`b2ec3f7`) added Step 2.55 in ``ToolRegistry.execute``: when a
tool_call hook rewrites the arguments, policy + pre-flight re-run on
the final args. As landed, the re-check honoured the per-call
``approved`` flag, so an approval given for the ORIGINAL arguments
silently carried to a rewritten call whose final verdict was "ask".

FUP-1 makes that binding a persisted user setting,
``UserConfig.approval_binding_mode``:

* ``"rebind"`` (DEFAULT, strict) — the Step 2.55 re-check does NOT
  honour the per-call ``approved`` flag: an "ask" verdict on the
  final args raises the requires-approval error even when the
  original call was approved. The approval was for the pre-hook
  arguments; the final version must be approved as itself.
* ``"carry"`` — exactly the pre-FUP-1 behaviour: the approval
  carries to the rewritten call.

In BOTH modes: deny always blocks, pre-flight is identical,
unchanged args skip the re-check, and the approval_cache
covering-grant path is untouched (a stored grant is vetted against
the final args by ``covering_grant`` itself).

The setting is wired live: ``execute_tool`` passes
``user_config.approval_binding_mode`` into every ``registry.execute``
call, and CONFIG_UPDATE_REQUEST applies it to the live config, so a
Settings change takes effect on the next tool call — no restart.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.tools import hooks
from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
    create_file_tools,
)


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class _Audit:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))


class _WorkerSpy:
    """Stands in for the sandbox worker: records calls, never runs."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        args = kwargs.get("args") or {}
        return f"worker-ran:{args.get('command', args.get('path', ''))}"


@pytest.fixture()
def worker_spy(monkeypatch):
    spy = _WorkerSpy()
    monkeypatch.setattr(
        "aether_engine.workers.sandbox.execute_tool_in_worker", spy)
    return spy


def _registry_with_file_tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    return registry


def _read_registry(ran: list) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file",
        description="x",
        execute_fn=lambda path: ran.append(path) or "body",
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


def _rewrite_hook(new_args):
    def _hook(tool_name, args, _context):
        return dict(new_args)

    return _hook


SSH_PATH = str(Path.home() / ".ssh" / "id_ed25519")


# ---------------------------------------------------------------------------
# (a) Rewrite -> final verdict "ask", original approved=True:
#     rebind raises requires-approval; carry executes.
# ---------------------------------------------------------------------------


async def test_rebind_rewrite_to_ask_raises_despite_approval():
    ran: list = []
    registry = _read_registry(ran)
    hooks.register_tool_call_hook("retarget", _rewrite_hook({"path": SSH_PATH}))
    audit = _Audit()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "read_file", {"path": "/tmp/benign-notes.txt"},
            workspace_roots=["/tmp"], approved=True,
            approval_binding="rebind", audit_logger=audit)
    assert "requires approval (policy=ask)" in str(excinfo.value)
    # The cause must be legible: the approval covered other arguments.
    assert "pre-hook" in str(excinfo.value)
    assert ran == []


async def test_carry_rewrite_to_ask_executes_with_approval():
    ran: list = []
    registry = _read_registry(ran)
    hooks.register_tool_call_hook("retarget", _rewrite_hook({"path": SSH_PATH}))
    result = await registry.execute(
        "read_file", {"path": "/tmp/benign-notes.txt"},
        workspace_roots=["/tmp"], approved=True,
        approval_binding="carry", audit_logger=_Audit())
    assert result == "body"
    assert ran == [SSH_PATH]


async def test_rebind_is_the_registry_default(worker_spy):
    # No approval_binding argument at all: unwired callers fail strict.
    registry = _registry_with_file_tools()
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"command": "echo rewritten"}))
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "execute_shell", {"command": "echo original"},
            approved=True, audit_logger=_Audit())
    assert "requires approval (policy=ask)" in str(excinfo.value)
    assert worker_spy.calls == []


async def test_carry_shell_rewrite_to_ask_executes(worker_spy):
    registry = _registry_with_file_tools()
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"command": "echo rewritten"}))
    result = await registry.execute(
        "execute_shell", {"command": "echo original"},
        approved=True, approval_binding="carry", audit_logger=_Audit())
    assert result == "worker-ran:echo rewritten"


# ---------------------------------------------------------------------------
# (b) Rewrite -> "deny": blocked in BOTH modes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["rebind", "carry"])
async def test_rewrite_to_deny_blocked_in_both_modes(worker_spy, mode):
    registry = _registry_with_file_tools()
    hooks.register_tool_call_hook(
        "escalate", _rewrite_hook({"command": "rm -rf /"}))
    audit = _Audit()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "execute_shell", {"command": "echo hi"},
            approved=True, approval_binding=mode, audit_logger=audit)
    assert "blocked by policy (deny)" in str(excinfo.value)
    assert worker_spy.calls == []


# ---------------------------------------------------------------------------
# (c) No rewrite: identical behaviour in both modes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["rebind", "carry"])
async def test_no_rewrite_approved_executes_in_both_modes(worker_spy, mode):
    registry = _registry_with_file_tools()
    result = await registry.execute(
        "execute_shell", {"command": "echo hi"},
        approved=True, approval_binding=mode, audit_logger=_Audit())
    assert result == "worker-ran:echo hi"


@pytest.mark.parametrize("mode", ["rebind", "carry"])
async def test_no_rewrite_unapproved_asks_in_both_modes(worker_spy, mode):
    registry = _registry_with_file_tools()
    with pytest.raises(ToolExecutionError) as excinfo:
        await registry.execute(
            "execute_shell", {"command": "echo hi"},
            approved=False, approval_binding=mode, audit_logger=_Audit())
    assert "requires approval (policy=ask)" in str(excinfo.value)
    assert worker_spy.calls == []


# ---------------------------------------------------------------------------
# Approval cache: a stored grant is vetted against the FINAL args by
# covering_grant itself, so it still covers a rewritten "ask" call in
# rebind mode (the per-call flag is what rebind drops, not grants).
# ---------------------------------------------------------------------------


class _StubCache:
    """covering_grant stand-in: covers only paths under /covered."""

    def __init__(self):
        self.seen_args = []

    def covering_grant(self, *, risk, args):
        self.seen_args.append(dict(args or {}))
        path = str((args or {}).get("path", ""))
        if path.startswith("/covered"):
            return {"id": 7, "prefix": "/covered"}
        return None


async def test_rebind_covering_grant_on_final_args_still_allows():
    ran: list = []
    registry = _read_registry(ran)
    target = "/covered/notes.txt"
    hooks.register_tool_call_hook("retarget", _rewrite_hook({"path": target}))
    cache = _StubCache()
    # policies force "ask" for read_file so the grant path is what
    # can allow the rewritten call; approved=True must NOT carry.
    result = await registry.execute(
        "read_file", {"path": "/tmp/benign-notes.txt"},
        workspace_roots=["/tmp", "/covered"], approved=True,
        policies={"read_file": "Require Approval"},
        approval_binding="rebind", approval_cache=cache,
        audit_logger=_Audit())
    assert result == "body"
    assert ran == [target]
    # The grant was consulted against the final (rewritten) args.
    assert cache.seen_args[-1] == {"path": target}


# ---------------------------------------------------------------------------
# (d) Config: round-trip, validation, default.
# ---------------------------------------------------------------------------


def test_config_default_is_rebind():
    assert UserConfig().approval_binding_mode == "rebind"


def test_config_roundtrip_preserves_carry():
    cfg = UserConfig(approval_binding_mode="carry")
    d = cfg.to_dict()
    assert d["approval_binding_mode"] == "carry"
    assert UserConfig.from_dict(d).approval_binding_mode == "carry"


def test_config_roundtrip_preserves_rebind():
    cfg = UserConfig(approval_binding_mode="rebind")
    assert UserConfig.from_dict(cfg.to_dict()).approval_binding_mode == "rebind"


@pytest.mark.parametrize("bad", ["strict", "CARRY", "", None, 42, True])
def test_config_invalid_value_falls_back_to_rebind(bad):
    assert UserConfig.from_dict(
        {"approval_binding_mode": bad}).approval_binding_mode == "rebind"


def test_config_missing_value_defaults_to_rebind():
    assert UserConfig.from_dict({}).approval_binding_mode == "rebind"


# ---------------------------------------------------------------------------
# (e) Websocket CONFIG_UPDATE: valid applies + persists; invalid ignored.
# Mirrors the settings_env harness of test_epic6_e6f05_settings_surfaces.
# ---------------------------------------------------------------------------


class DummyProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=DummyProtector())
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    ws_root = tmp_path / "wsroot"
    ws_root.mkdir()

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    save_config(UserConfig(workspace_roots=[str(ws_root)]), config_path)

    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry([]))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    return {"tmp_path": tmp_path, "config_path": config_path,
            "ws_root": ws_root, "store": store}


def _recv_until(ws, event_type, max_events=12):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == event_type:
            return ev
    raise AssertionError(f"no {event_type} received")


class _Socket:
    def __enter__(self):
        self._client = TestClient(app)
        self._client.__enter__()
        self._cm = self._client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        self.ws = self._cm.__enter__()
        hello = Event.from_json(self.ws.receive_text())
        assert hello.type == EventType.HELLO
        return self.ws

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._client.__exit__(*exc)
        return False


def _update(ws, config):
    ws.send_text(Event(
        type=EventType.CONFIG_UPDATE_REQUEST,
        payload={"config": config}).to_json())
    return _recv_until(ws, EventType.CONFIG_UPDATE_RESPONSE)


def test_ws_config_update_applies_and_persists_carry(settings_env):
    with _Socket() as ws:
        resp = _update(ws, {"approval_binding_mode": "carry"})
        assert resp.payload["success"] is True
        assert resp.payload["config"]["approval_binding_mode"] == "carry"
    # Live state (read per call by execute_tool) and disk both moved.
    assert engine_state.user_config.approval_binding_mode == "carry"
    on_disk = load_config(settings_env["config_path"])
    assert on_disk.approval_binding_mode == "carry"


def test_ws_config_update_applies_rebind(settings_env):
    with _Socket() as ws:
        _update(ws, {"approval_binding_mode": "carry"})
        resp = _update(ws, {"approval_binding_mode": "rebind"})
        assert resp.payload["success"] is True
        assert resp.payload["config"]["approval_binding_mode"] == "rebind"
    assert engine_state.user_config.approval_binding_mode == "rebind"


def test_ws_config_update_invalid_value_ignored(settings_env):
    with _Socket() as ws:
        resp = _update(ws, {"approval_binding_mode": "sometimes"})
        assert resp.payload["success"] is True
        # Ignored, matching the handler's style for the tuning fields:
        # the stored value stays at the strict default.
        assert resp.payload["config"]["approval_binding_mode"] == "rebind"
    assert engine_state.user_config.approval_binding_mode == "rebind"
    on_disk = load_config(settings_env["config_path"])
    assert on_disk.approval_binding_mode == "rebind"
