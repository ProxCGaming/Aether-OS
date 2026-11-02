"""Epic 3 — MCP completion.

E3-01: the owner's Demo 001 as an automated end-to-end proof — fake
server -> MCPRegistry -> build_tool_registry -> supervisor graph ->
planner calls fake__add -> approval pause -> resume -> result 8 —
plus the supervisor's non-memory tool-call drop being audible
(issue 012's failure class: never silent again).
E3-02: mcp_tool_risk_overrides change a tool's classification
everywhere (registry build, plan-mode eligibility, policy default),
invalid entries are cleaned/ignored, and the project config layer
cannot set them.
E3-04: misbehaving servers are bounded — a server that accepts the
connection but never answers initialize fails the connect with a
real error inside its timeout; a hanging tool call returns an error
string inside the tool timeout.
"""
import asyncio
import logging
import socket
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage  # noqa: F401  (parity harness parity)

import aether_engine.langgraph.executor as executor_module
import aether_engine.langgraph.nodes.planner as planner_module
import aether_engine.memory.episodic as episodic_module
import aether_engine.memory.knowledge_graph as kg_module
from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import ToolRisk, build_tool_registry
from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def mcp_registry(tmp_path, monkeypatch):
    import aether_engine.mcp.registry as reg_module

    monkeypatch.setattr(reg_module, "_MCP_CONFIG_PATH", tmp_path / "mcp_servers.json")
    from aether_engine.mcp.registry import MCPRegistry

    return MCPRegistry()


@pytest.fixture()
async def connected(mcp_registry, fake_url):
    mcp_registry.add_server("fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10})
    result = await mcp_registry.connect_server("fake")
    assert result["ok"] is True, result
    yield mcp_registry
    await mcp_registry.disconnect_server("fake")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", _async_none)
    monkeypatch.setattr(kg_module, "extract_and_store_facts", _async_none)
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "e3/artifact.txt")
    return tmp_path


async def _async_none(*args, **kwargs):
    return None


async def _collect(agen):
    return [event async for event in agen]


class MCPDemoProvider:
    """Supervisor routes to planner; planner's first call uses
    fake__add(3, 5), later calls answer with the final text."""

    def __init__(self):
        self.planner_messages = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self.planner_messages.append([dict(m) for m in messages])
            if len(self.planner_messages) == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name="fake__add", args={"a": 3, "b": 5}, call_id="mcp-demo-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text="3 + 5 = 8.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "demo routing", "briefing": ""}',
            finish_reason="stop",
        )


# ----------------------------------------------------------------------
# E3-01 — the demo flow, end to end
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_demo_flow_through_graph_with_approval(graph_env, connected):
    registry = build_tool_registry(mcp_tools=connected.get_mcp_tools())
    provider = MCPDemoProvider()
    session_id = "e3-demo"

    events = await _collect(executor_module.run_langgraph_task(
        "use the fake add tool to compute 3 + 5",
        provider,
        tools=registry,
        task_id="task-e3-demo",
        request_id="req-e3-demo",
        session_id=session_id,
        user_config=UserConfig(),
    ))
    types = [ev.type for ev in events]
    assert EventType.TOOL_APPROVAL_REQUEST in types, "MCP tools must pause for approval by default"
    approval = next(ev for ev in events if ev.type == EventType.TOOL_APPROVAL_REQUEST)
    assert approval.payload["tool_name"] == "fake__add"
    assert approval.payload["args"] == {"a": 3, "b": 5}
    assert EventType.TASK_COMPLETED not in types

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))
    resume_types = [ev.type for ev in resume_events]
    assert EventType.TASK_COMPLETED in resume_types
    completed = next(ev for ev in resume_events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    # The planner's second call saw the tool result: 8.
    tool_results = [
        m.get("content", "")
        for call in provider.planner_messages[1:]
        for m in call
        if m.get("role") == "tool"
    ]
    assert any("8" in str(c) for c in tool_results), tool_results


class DroppingSupervisorProvider:
    """The supervisor model tries to call fake__add itself (it is not
    offered it — the drop path) while also returning a routing
    decision as text."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "supervisor":
            yield StreamChunk(
                text='{"next": "planner", "reason": "routing on", "briefing": ""}',
                tool_calls=[ToolCall(name="fake__add", args={"a": 1, "b": 2}, call_id="drop-1")],
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(text="specialist answer", finish_reason="stop")


@pytest.mark.asyncio
async def test_supervisor_non_memory_call_drop_is_audible(graph_env, connected, caplog):
    registry = build_tool_registry(mcp_tools=connected.get_mcp_tools())
    provider = DroppingSupervisorProvider()
    with caplog.at_level(logging.WARNING, logger="aether_engine.langgraph.supervisor_model"):
        events = await _collect(executor_module.run_langgraph_task(
            "compute something",
            provider,
            tools=registry,
            task_id="task-e3-drop",
            request_id="req-e3-drop",
            session_id="e3-drop",
            user_config=UserConfig(),
        ))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    assert any("fake__add" in r.message and "dropped" in r.message for r in caplog.records), (
        "the dropped supervisor tool call must be logged by name"
    )


# ----------------------------------------------------------------------
# E3-02 — risk overrides
# ----------------------------------------------------------------------

def test_risk_override_changes_classification(connected):
    default = {t.name: t for t in connected.get_mcp_tools()}
    assert default["fake__add"].risk == ToolRisk.EXECUTE

    overridden = {t.name: t for t in connected.get_mcp_tools(risk_overrides={"fake__add": "read_only"})}
    assert overridden["fake__add"].risk == ToolRisk.READ_ONLY
    # The cached default is untouched (copies, not mutation).
    assert {t.name: t for t in connected.get_mcp_tools()}["fake__add"].risk == ToolRisk.EXECUTE

    # Plan-mode eligibility + policy default follow the override.
    from aether_engine.tools.policy import resolve_tool_policy

    registry = build_tool_registry(mcp_tools=list(overridden.values()))
    assert "fake__add" in registry.read_only_names()
    assert resolve_tool_policy(overridden["fake__add"], {}) == "allow"
    assert resolve_tool_policy(default["fake__add"], {}) == "ask"

    registry_default = build_tool_registry(mcp_tools=connected.get_mcp_tools())
    assert "fake__add" not in registry_default.read_only_names()


def test_risk_override_invalid_values_ignored(connected):
    tools = {t.name: t for t in connected.get_mcp_tools(
        risk_overrides={"fake__add": "bogus-risk", "nope__missing": "read_only"})}
    assert tools["fake__add"].risk == ToolRisk.EXECUTE


def test_user_config_overrides_round_trip_and_cleaning():
    cfg = UserConfig.from_dict({
        "mcp_tool_risk_overrides": {
            "fake__add": "read_only",
            "bad_entry": "read_only",       # no server__tool shape
            "fake__mul": "sometimes",       # invalid risk
            "fake__del": "destructive",
        }
    })
    assert cfg.mcp_tool_risk_overrides == {"fake__add": "read_only", "fake__del": "destructive"}
    again = UserConfig.from_dict(cfg.to_dict())
    assert again.mcp_tool_risk_overrides == cfg.mcp_tool_risk_overrides
    assert UserConfig().mcp_tool_risk_overrides == {}


def test_project_layer_cannot_set_risk_overrides(tmp_path):
    import json

    from aether_engine.config_layers import effective_user_config

    config_dir = tmp_path / ".aether"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "mcp_tool_risk_overrides": {"fake__add": "read_only"},
        "compaction_trigger_messages": 7,
    }), "utf-8")
    merged = effective_user_config(UserConfig(), workspace_root=str(tmp_path))
    assert merged.mcp_tool_risk_overrides == {}, "a cloned workspace must not reclassify MCP tools"
    assert merged.compaction_trigger_messages == 7


# ----------------------------------------------------------------------
# E3-04 — misbehaving servers are bounded
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hanging_initialize_fails_connect_within_timeout(mcp_registry):
    """A raw socket that accepts and never speaks: connect must fail
    with a real error inside the configured timeout, state clean."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                # Hold the connection open, say nothing.
                threading.Timer(30.0, conn.close).start()
            except TimeoutError:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        mcp_registry.add_server("hang", {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "timeout": 2,
        })
        started = time.monotonic()
        result = await mcp_registry.connect_server("hang")
        elapsed = time.monotonic() - started
        assert result["ok"] is False, result
        assert result.get("error"), "a real error must surface"
        assert elapsed < 20, f"connect took {elapsed:.1f}s against a 2s timeout"
        status = mcp_registry.get_server_status("hang")
        assert not status.get("connected"), status
    finally:
        stop.set()
        listener.close()


@pytest.mark.asyncio
async def test_hanging_tool_call_is_bounded(connected, monkeypatch):
    # A dedicated slow server (uvicorn thread, the repo fixture's
    # pattern): its only tool sleeps far past the tool timeout.
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    import aether_engine.mcp.registry as reg_module

    slow = FastMCP("slow")

    @slow.tool()
    def hang(seconds: int = 30) -> str:
        time.sleep(seconds)
        return "never"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(
        slow.streamable_http_app(), host="127.0.0.1", port=free_port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            try:
                probe = socket.create_connection(("127.0.0.1", free_port), timeout=1)
                probe.close()
                break
            except OSError:
                await asyncio.sleep(0.2)
        from aether_engine.mcp.registry import MCPRegistry

        slow_registry = MCPRegistry()
        slow_registry.add_server("slow", {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{free_port}/mcp",
            "timeout": 10,
        })
        result = await slow_registry.connect_server("slow")
        assert result["ok"] is True, result
        monkeypatch.setattr(reg_module, "MCP_TOOL_TIMEOUT_S", 2)
        tools = {t.name: t for t in slow_registry.get_mcp_tools()}
        started = time.monotonic()
        out = await tools["slow__hang"].execute_fn(seconds=30)
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"tool call took {elapsed:.1f}s against a 2s timeout"
        assert "error" in out.lower() or "timed out" in out.lower() or "timeout" in out.lower(), out
        await slow_registry.disconnect_server("slow")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
