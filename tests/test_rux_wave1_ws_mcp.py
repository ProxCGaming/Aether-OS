"""Wave 1, Slice 2c — RUX-B-04: the MCP handler family off the WS loop.

Every MCP branch (server connect/disconnect/test/remove/update,
resource list/read, prompt list/run) used to await the registry
inline in the message loop at the server's connect timeout (30 s
default, up to 300 s), so one hung server froze all control traffic
on the socket. Now each branch answers from a background task:

- Loop responsiveness: an INSTANT message (MODEL_SET_DEFAULT) sent
  right behind an MCP request whose registry call hangs is answered
  BEFORE the MCP response.
- Contract: response type + payload are exactly what the inline
  handlers produced (success and failure shapes alike).
- Race guard: operations on the SAME server stay serialised (the
  per-server lock restores what the serial loop guaranteed), while
  operations on DIFFERENT servers run in parallel.

Harness follows the ws_env pattern of test_rux_wave1_ws_discovery.py,
plus a real MCPRegistry on a tmp config file patched in as
GLOBAL_MCP_REGISTRY (as in test_mcp_websocket_handlers.py), with the
registry methods stubbed at the registry boundary — no live servers,
no network.
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.mcp.registry as registry_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.mcp.registry import MCPConfigError, MCPRegistry
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat", "code", "tools"], priority=1),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-main", label="Claude Main",
                   capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = UserConfig(
        default_provider="google_gemini", default_model="gemini-main",
        workspace_roots=[str(tmp_path)])
    save_config(cfg, config_path)
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ws_module, "classify_intent", AsyncMock(return_value=None))
    registry = _registry()
    monkeypatch.setattr(engine_state, "model_registry", registry)
    monkeypatch.setattr(
        engine_state, "routing_policy", RoutingPolicy(registry=registry))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(engine_state, "user_config", cfg)
    # A real MCP registry on a tmp config, patched in as the global —
    # the handlers import it lazily from the registry module.
    mcp_registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json")
    # auto_connect off: the app lifespan must not connect these before
    # a test runs (the methods are stubbed per-test anyway).
    for name in ("fake", "fake2"):
        mcp_registry.add_server(name, {
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9/mcp",
            "timeout": 5,
            "auto_connect": False,
        })
    monkeypatch.setattr(registry_module, "GLOBAL_MCP_REGISTRY", mcp_registry)
    yield {"tmp_path": tmp_path, "config": cfg, "mcp": mcp_registry}


def _connect_ws(stack):
    client = stack.enter_context(TestClient(app))
    ws = stack.enter_context(client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ))
    ws.receive_text()  # HELLO
    return ws


@pytest.fixture
def stack():
    with ExitStack() as s:
        yield s


def _send(ws, type_, payload=None):
    ws.send_text(Event(type=type_, payload=payload or {}).to_json())


def _receive_until(ws, type_, limit=15):
    for _ in range(limit):
        ev = Event.from_json(ws.receive_text())
        if ev.type == type_:
            return ev
    raise AssertionError(f"never received {type_}")


def _set_default_event():
    return Event(
        type=EventType.MODEL_SET_DEFAULT,
        payload={"provider": "google_gemini", "model": "gemini-2.5-pro"},
    )


# ---------------------------------------------------------------------------
# Loop responsiveness — the RUX-B-04 defect itself
# ---------------------------------------------------------------------------


class TestMcpHandlersOffLoop:
    def test_instant_message_answered_before_hanging_connect(self, ws_env, stack):
        async def hang_connect(name):
            await asyncio.sleep(3)
            return {"ok": False, "error": "connect timed out (stub)"}

        stack.enter_context(patch.object(
            ws_env["mcp"], "connect_server", hang_connect))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        ws.send_text(_set_default_event().to_json())

        seen = []
        connect_ev = None
        for _ in range(10):
            ev = Event.from_json(ws.receive_text())
            seen.append(ev.type)
            if ev.type == EventType.MCP_SERVER_CONNECT_RESPONSE:
                connect_ev = ev
            if connect_ev is not None and EventType.MODEL_DEFAULT_CHANGED in seen:
                break

        assert EventType.MODEL_DEFAULT_CHANGED in seen
        assert connect_ev is not None
        # The whole point of RUX-B-04: the instant message is NOT
        # stuck behind the hanging MCP connect.
        assert seen.index(EventType.MODEL_DEFAULT_CHANGED) < seen.index(
            EventType.MCP_SERVER_CONNECT_RESPONSE)
        assert connect_ev.payload == {
            "success": False, "name": "fake",
            "error": "connect timed out (stub)",
            "status": "disconnected", "tool_count": 0,
        }

    def test_instant_message_answered_before_hanging_test(self, ws_env, stack):
        async def hang_test(name):
            await asyncio.sleep(3)
            return {"ok": False, "error": "test timed out (stub)", "tools": []}

        stack.enter_context(patch.object(
            ws_env["mcp"], "test_connection", hang_test))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_TEST_REQUEST, {"name": "fake"})
        ws.send_text(_set_default_event().to_json())

        seen = []
        test_ev = None
        for _ in range(10):
            ev = Event.from_json(ws.receive_text())
            seen.append(ev.type)
            if ev.type == EventType.MCP_SERVER_TEST_RESPONSE:
                test_ev = ev
            if test_ev is not None and EventType.MODEL_DEFAULT_CHANGED in seen:
                break

        assert EventType.MODEL_DEFAULT_CHANGED in seen
        assert test_ev is not None
        assert seen.index(EventType.MODEL_DEFAULT_CHANGED) < seen.index(
            EventType.MCP_SERVER_TEST_RESPONSE)
        assert test_ev.payload == {
            "success": False, "name": "fake",
            "error": "test timed out (stub)", "tools": [],
        }


# ---------------------------------------------------------------------------
# Contract — response shapes identical to the inline handlers
# ---------------------------------------------------------------------------


class TestMcpResponseContract:
    def test_connect_success_payload(self, ws_env, stack):
        async def ok_connect(name):
            return {"ok": True, "error": None}

        stack.enter_context(patch.object(
            ws_env["mcp"], "connect_server", ok_connect))
        stack.enter_context(patch.object(
            ws_env["mcp"], "get_server_status",
            lambda name: {"status": "connected", "tool_count": 2, "error": None}))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_SERVER_CONNECT_RESPONSE)
        assert ev.payload == {
            "success": True, "name": "fake", "error": None,
            "status": "connected", "tool_count": 2,
        }

    def test_test_success_and_failure_payloads(self, ws_env, stack):
        async def ok_test(name):
            return {"ok": True, "error": None, "tools": ["add", "subtract"]}

        stack.enter_context(patch.object(
            ws_env["mcp"], "test_connection", ok_test))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_TEST_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_SERVER_TEST_RESPONSE)
        assert ev.payload == {
            "success": True, "name": "fake", "error": None,
            "tools": ["add", "subtract"],
        }

    def test_test_failure_payload(self, ws_env, stack):
        async def fail_test(name):
            return {"ok": False, "error": "boom", "tools": []}

        stack.enter_context(patch.object(
            ws_env["mcp"], "test_connection", fail_test))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_TEST_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_SERVER_TEST_RESPONSE)
        assert ev.payload == {
            "success": False, "name": "fake", "error": "boom", "tools": [],
        }

    def test_resource_list_failure_payload(self, ws_env, stack):
        async def fail_list(name):
            raise RuntimeError("server gone")

        stack.enter_context(patch.object(
            ws_env["mcp"], "list_resources", fail_list))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_RESOURCE_LIST_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_RESOURCE_LIST_RESPONSE)
        assert ev.payload == {
            "success": False, "name": "fake", "resources": [],
            "error": "server gone",
        }

    def test_prompt_list_success_payload(self, ws_env, stack):
        async def ok_prompts(name):
            return [{"name": "greet", "description": "Say hi", "arguments": []}]

        stack.enter_context(patch.object(
            ws_env["mcp"], "list_prompts", ok_prompts))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_PROMPT_LIST_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_PROMPT_LIST_RESPONSE)
        assert ev.payload == {
            "success": True, "name": "fake",
            "prompts": [{"name": "greet", "description": "Say hi", "arguments": []}],
            "error": "",
        }

    def test_remove_config_error_payload(self, ws_env, stack):
        async def fail_remove(name):
            raise MCPConfigError("config is corrupt")

        stack.enter_context(patch.object(
            ws_env["mcp"], "remove_server", fail_remove))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_REMOVE_REQUEST, {"name": "fake"})
        ev = _receive_until(ws, EventType.MCP_SERVER_REMOVE_RESPONSE)
        assert ev.payload == {
            "success": False, "name": "fake", "error": "config is corrupt",
        }

    def test_update_success_payload(self, ws_env, stack):
        async def ok_update(name, *, auto_connect=None, enabled=None):
            return {"transport": "streamable_http", "enabled": False}

        stack.enter_context(patch.object(
            ws_env["mcp"], "update_server", ok_update))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_UPDATE_REQUEST,
              {"name": "fake", "enabled": False})
        ev = _receive_until(ws, EventType.MCP_SERVER_UPDATE_RESPONSE)
        assert ev.payload == {
            "success": True, "name": "fake",
            "config": {"transport": "streamable_http", "enabled": False},
            "error": "",
        }

    def test_disconnect_missing_name_payload(self, ws_env, stack):
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {})
        ev = _receive_until(ws, EventType.MCP_SERVER_DISCONNECT_RESPONSE)
        assert ev.payload == {
            "success": False, "name": None,
            "error": "Server name is required.",
            "status": None, "tool_count": 0,
        }


# ---------------------------------------------------------------------------
# Race guard — same server serialised, different servers parallel
# ---------------------------------------------------------------------------


class TestMcpPerServerSerialisation:
    def test_same_server_operations_do_not_overlap(self, ws_env, stack):
        state = {"active": 0, "max_active": 0, "order": []}

        def _tracked(label, delay, result):
            async def _op(name):
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["order"].append(f"{label}-start")
                await asyncio.sleep(delay)
                state["order"].append(f"{label}-end")
                state["active"] -= 1
                return result
            return _op

        stack.enter_context(patch.object(
            ws_env["mcp"], "connect_server",
            _tracked("connect", 0.6, {"ok": False, "error": "stub"})))
        stack.enter_context(patch.object(
            ws_env["mcp"], "disconnect_server",
            _tracked("disconnect", 0.1, {"ok": True, "error": None})))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        _send(ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {"name": "fake"})
        _receive_until(ws, EventType.MCP_SERVER_CONNECT_RESPONSE)
        _receive_until(ws, EventType.MCP_SERVER_DISCONNECT_RESPONSE)

        # The per-server lock restores the serial loop's guarantee:
        # the disconnect did not start until the connect finished.
        assert state["max_active"] == 1
        assert state["order"] == [
            "connect-start", "connect-end",
            "disconnect-start", "disconnect-end",
        ]

    def test_different_servers_run_in_parallel(self, ws_env, stack):
        async def timed_connect(name):
            await asyncio.sleep(2.0 if name == "fake" else 0.2)
            return {"ok": False, "error": "stub"}

        stack.enter_context(patch.object(
            ws_env["mcp"], "connect_server", timed_connect))
        ws = _connect_ws(stack)
        _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        _send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake2"})

        names = []
        for _ in range(10):
            ev = Event.from_json(ws.receive_text())
            if ev.type == EventType.MCP_SERVER_CONNECT_RESPONSE:
                names.append(ev.payload["name"])
            if len(names) == 2:
                break

        # fake2's fast connect answers long before fake's slow one —
        # impossible under the old serial loop, where fake's response
        # would have come first simply because it was sent first.
        assert names == ["fake2", "fake"]
