"""E3-F01 + E3-F02: MCP tool identity and START_TASK risk overlays.

F01: converted MCP tools are named ``server__tool`` by unescaped
concatenation, and server names may themselves contain ``__`` — so
``trusted__docs`` + ``read`` and ``trusted`` + ``docs__read`` produce
the SAME name. Before the fix, ``get_mcp_tools()`` returned both,
a name-keyed risk override classified both, and the shared registry
silently kept whichever registered last: calling
``trusted__docs__read`` executed the OTHER server's tool under the
first server's trusted identity.

F02: START_TASK converted MCP tools with the GLOBAL
``mcp_tool_risk_overrides`` while the task's effective config
(global → project → per-task overlay, E1-14) was resolved separately
and forwarded to the graph — so a task-level tightening (or
loosening) of an MCP tool's risk never reached the registry the
policy pipeline actually sees. The approval-resume path already
uses the resumed task's effective overrides (E1-F04); task start
must resolve them the same way.
"""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import aether_engine.mcp.registry as mcp_registry_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk, ToolCall  # noqa: F401  (house parity)
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import (
    Tool,
    ToolRegistry,
    ToolRisk,
    build_tool_registry,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeMCPClient:
    """Stands in for MCPClientManager: serves fixed tool lists per
    server and records every call_tool so tests can see WHICH server
    a converted tool actually executed on."""

    def __init__(self, tools_by_server):
        self.tools_by_server = tools_by_server
        self.calls = []

    async def list_tools(self, server):
        return [
            SimpleNamespace(
                name=name,
                description=f"{server}:{name}",
                inputSchema={"type": "object", "properties": {}},
            )
            for name in self.tools_by_server[server]
        ]

    async def call_tool(self, server, tool, args, timeout=None):
        self.calls.append((server, tool))
        return f"EXECUTED_BY:{server}"


@pytest.fixture()
def mcp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mcp_registry_module, "_MCP_CONFIG_PATH", tmp_path / "mcp_servers.json")
    return MCPRegistry()


async def _connected_registry(registry, tools_by_server):
    fake = FakeMCPClient(tools_by_server)
    registry._client = fake
    for server in tools_by_server:
        registry.add_server(server, {"transport": "stdio", "command": "fake"})
        await registry._refresh_tools(server)
    return fake


# ---------------------------------------------------------------------------
# F01 — cross-server name collision
# ---------------------------------------------------------------------------

COLLIDING = {
    "trusted__docs": ["read", "fetch"],
    "trusted": ["docs__read", "ping"],
}
AMBIGUOUS = "trusted__docs__read"


@pytest.mark.asyncio
async def test_colliding_tools_are_refused_not_substituted(mcp_registry, caplog):
    fake = await _connected_registry(mcp_registry, COLLIDING)

    with caplog.at_level(logging.ERROR, logger="aether_engine.mcp.registry"):
        tools = mcp_registry.get_mcp_tools()

    names = [t.name for t in tools]
    assert AMBIGUOUS not in names, (
        f"ambiguous name still offered: {names}")
    # The non-colliding tools from both servers survive untouched.
    assert "trusted__docs__fetch" in names
    assert "trusted__ping" in names
    # The refusal is loud: an error names the collision.
    assert any(AMBIGUOUS in rec.message for rec in caplog.records), (
        "collision must be logged as an error, not silently dropped")

    # And nothing executable remains under the ambiguous name.
    built = build_tool_registry(mcp_tools=tools)
    assert AMBIGUOUS not in [t.name for t in built.get_tools()]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_risk_override_cannot_transfer_across_a_collision(mcp_registry):
    await _connected_registry(mcp_registry, COLLIDING)

    tools = mcp_registry.get_mcp_tools(
        risk_overrides={AMBIGUOUS: "read_only", "trusted__ping": "read_only"})
    by_name = {t.name: t for t in tools}

    assert AMBIGUOUS not in by_name, (
        "the override classified a tool under an ambiguous identity")
    # The same override mechanism still works for unambiguous names.
    assert by_name["trusted__ping"].risk == ToolRisk.READ_ONLY
    assert by_name["trusted__docs__fetch"].risk == ToolRisk.EXECUTE


@pytest.mark.asyncio
async def test_same_server_duplicate_tool_names_are_refused(mcp_registry):
    await _connected_registry(mcp_registry, {"solo": ["read", "read", "ping"]})

    names = [t.name for t in mcp_registry.get_mcp_tools()]
    assert "solo__read" not in names, (
        "a server returning the same tool twice leaves an ambiguous identity")
    assert "solo__ping" in names


@pytest.mark.asyncio
async def test_unambiguous_tool_still_executes_on_its_owner(mcp_registry):
    fake = await _connected_registry(mcp_registry, {"fake": ["add"]})

    built = build_tool_registry(mcp_tools=mcp_registry.get_mcp_tools())
    tool = built.get_tool("fake__add")
    assert tool.source == "mcp:fake"
    assert await tool.execute_fn(a=3, b=5) == "EXECUTED_BY:fake"
    assert fake.calls == [("fake", "add")]


def test_registry_refuses_cross_source_name_replacement():
    registry = ToolRegistry()
    registry.register(Tool(name="srv__tool", description="a",
                           source="mcp:a", execute_fn=lambda **kw: "a"))
    with pytest.raises(ValueError, match="srv__tool"):
        registry.register(Tool(name="srv__tool", description="b",
                               source="mcp:b", execute_fn=lambda **kw: "b"))
    # The original registration is untouched.
    assert registry.get_tool("srv__tool").source == "mcp:a"
    # Same-source re-registration (a refresh) stays legal.
    registry.register(Tool(name="srv__tool", description="a2",
                           source="mcp:a", execute_fn=lambda **kw: "a2"))
    assert registry.get_tool("srv__tool").execute_fn() == "a2"


# ---------------------------------------------------------------------------
# F02 — START_TASK resolves MCP risk from the task's effective config
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Isolate a websocket test (pattern from
    test_e1_approval_resume_ws.py): tmp token/audit/session/checkpoint
    stores, no real memory writes, engine globals saved/restored."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "e3/artifact.txt")
    monkeypatch.setattr(
        ws_module, "rewind_service",
        SimpleNamespace(commit=AsyncMock(return_value=None)))

    saved = (engine_state.user_config, engine_state.active_provider,
             engine_state.active_model)
    yield SimpleNamespace(tmp_path=tmp_path)
    (engine_state.user_config, engine_state.active_provider,
     engine_state.active_model) = saved


def _drive_start_task(payload, setup=None, timeout=60):
    """Send START_TASK on a fresh connection; collect frames until
    the task settles. Bounded thread, same reason as the E1 resume
    harness: a handler that dies mid-start must fail, not hang.

    ``setup`` runs after the app lifespan (which reloads
    engine_state.user_config from disk — assignments made before
    TestClient starts are wiped) and before START_TASK is sent."""
    outcome = {}

    def _run():
        frames = []
        try:
            with TestClient(app) as client:
                # Read live after startup, as test_engine_ws does: the
                # app lifespan re-initialises auth, so a token captured
                # before TestClient starts may be stale.
                token = engine_state.auth_token
                with client.websocket_connect(
                        "/ws/tasks",
                        headers={"Authorization": f"Bearer {token}"}) as ws:
                    hello = Event.from_json(ws.receive_text())
                    assert hello.type == EventType.HELLO
                    if setup is not None:
                        setup()
                    ws.send_text(Event(
                        type=EventType.START_TASK,
                        request_id="start-req-1",
                        payload=payload,
                    ).to_json())
                    for _ in range(40):
                        ev = Event.from_json(ws.receive_text())
                        frames.append(ev)
                        if ev.type in (EventType.TASK_COMPLETED,
                                       EventType.TASK_FAILED):
                            break
            outcome["frames"] = frames
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            outcome["raise"] = exc

    thread = threading.Thread(target=_run, daemon=True, name="e3-f02-drive")
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "START_TASK never settled"
    if "raise" in outcome:
        raise outcome["raise"]
    return outcome["frames"]


def _start_with_overlay(ws_env, monkeypatch, *, global_map, overlay_map):
    """Run one START_TASK against a real MCPRegistry holding
    srv__probe; return the run_langgraph_task kwargs the route
    recorded (tools registry + effective user_config)."""
    registry = MCPRegistry()
    fake = FakeMCPClient({"srv": ["probe"]})
    registry._client = fake
    registry.add_server("srv", {"transport": "stdio", "command": "fake",
                                "auto_connect": False})
    asyncio.run(registry._refresh_tools("srv"))
    monkeypatch.setattr(
        mcp_registry_module, "GLOBAL_MCP_REGISTRY", registry)

    def _setup():
        engine_state.user_config = UserConfig(
            default_provider="taskprov",
            default_model="task-model",
            mcp_tool_risk_overrides=dict(global_map),
        )
        engine_state.active_provider = "taskprov"
        engine_state.active_model = "task-model"

    monkeypatch.setattr(
        ws_module, "_create_provider_instance",
        lambda name, model, secret_store, **kw: SimpleNamespace(
            provider_name=name, model=model))
    monkeypatch.setattr(
        ws_module, "classify_intent",
        AsyncMock(return_value=SimpleNamespace(is_chat=False)))

    recorded = {}

    async def fake_run(prompt, **kwargs):
        recorded.update(kwargs)
        yield Event(
            type=EventType.TASK_COMPLETED,
            request_id="start-req-1",
            payload={"task_id": "task-f02", "state": "succeeded",
                     "response": "done"},
        )

    def fake_run_factory(prompt, **kwargs):
        return fake_run(prompt, **kwargs)

    monkeypatch.setattr(ws_module, "run_langgraph_task", fake_run_factory)

    session_id = session_store.create_session("E3-F02 start test")
    payload = {"prompt": "please run the probe", "session_id": session_id}
    if overlay_map is not None:
        payload["config_overlay"] = {"mcp_tool_risk_overrides": overlay_map}
    frames = _drive_start_task(payload, setup=_setup)
    types = [ev.type for ev in frames]
    assert EventType.TASK_COMPLETED in types, f"task did not complete: {types}"
    assert "tools" in recorded, "route never handed a registry to the graph"
    return recorded


def test_start_task_honours_overlay_tightening(ws_env, monkeypatch):
    recorded = _start_with_overlay(
        ws_env, monkeypatch,
        global_map={"srv__probe": "execute"},
        overlay_map={"srv__probe": "read_only"},
    )
    tool = recorded["tools"].get_tool("srv__probe")
    assert tool.risk == ToolRisk.READ_ONLY, (
        "START_TASK classified the MCP tool from the GLOBAL map; the "
        "task overlay's stricter read_only never reached conversion")
    assert recorded["user_config"].mcp_tool_risk_overrides == {
        "srv__probe": "read_only"}


def test_start_task_honours_overlay_loosening(ws_env, monkeypatch):
    recorded = _start_with_overlay(
        ws_env, monkeypatch,
        global_map={"srv__probe": "read_only"},
        overlay_map={"srv__probe": "execute"},
    )
    tool = recorded["tools"].get_tool("srv__probe")
    assert tool.risk == ToolRisk.EXECUTE, (
        "START_TASK classified the MCP tool from the GLOBAL map; the "
        "task overlay's execute never reached conversion")


def test_start_task_without_overlay_uses_global_map(ws_env, monkeypatch):
    recorded = _start_with_overlay(
        ws_env, monkeypatch,
        global_map={"srv__probe": "read_only"},
        overlay_map=None,
    )
    tool = recorded["tools"].get_tool("srv__probe")
    assert tool.risk == ToolRisk.READ_ONLY
