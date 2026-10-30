"""Issue 001: build_tool_registry() + in-process MCP execution.

TDD red first: fails until aether_engine/tools/registry.py gains
build_tool_registry() and the MCP in-process special case, and
execute_tool_node routes MCP tools in-process instead of to the worker.
"""
import pytest

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
async def connected_tools(mcp_registry, fake_url):
    # Async fixture on purpose: the MCP session is bound to the event loop
    # it was created in, so connect and test must share one loop.
    mcp_registry.add_server(
        "fake", {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    )
    result = await mcp_registry.connect_server("fake")
    assert result["ok"] is True, result
    yield mcp_registry.get_mcp_tools()
    await mcp_registry.disconnect_server("fake")


def test_build_tool_registry_includes_builtins_and_mcp(connected_tools):
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry(mcp_tools=connected_tools)
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "get_current_time" in names
    assert "write_file" in names
    assert "search_web" in names
    assert "fake__add" in names


def test_build_tool_registry_can_exclude_file_tools(connected_tools):
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry(mcp_tools=connected_tools, include_file_tools=False)
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "write_file" not in names
    assert "execute_shell" not in names
    assert "fake__add" in names
    assert "get_current_time" in names


async def test_mcp_tool_executes_in_process_not_via_worker(connected_tools, monkeypatch):
    """The worker hardcodes 3 tools and no-ops anything else — if an MCP
    tool were routed there, the call would silently do nothing. It must
    run in-process instead."""
    from aether_engine.tools.registry import build_tool_registry

    def _worker_must_not_run(*args, **kwargs):
        raise AssertionError("aether_worker must not be called for MCP tools")

    monkeypatch.setattr(
        "aether_engine.workers.sandbox.execute_tool_in_worker", _worker_must_not_run
    )
    registry = build_tool_registry(mcp_tools=connected_tools)
    result = await registry.execute("fake__add", {"a": 3, "b": 5}, approved=True)
    assert result.strip() == "8"


async def test_mcp_tool_requires_approval_by_default(connected_tools):
    from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

    registry = build_tool_registry(mcp_tools=connected_tools)
    with pytest.raises(ToolExecutionError, match="approval"):
        await registry.execute("fake__add", {"a": 3, "b": 5})


async def test_execute_tool_node_runs_mcp_tool_in_process(connected_tools, monkeypatch):
    from aether_engine.config import UserConfig
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
    from aether_engine.tools.registry import build_tool_registry

    def _worker_must_not_run(*args, **kwargs):
        raise AssertionError("aether_worker must not be called for MCP tools")

    monkeypatch.setattr(
        "aether_engine.workers.sandbox.execute_tool_in_worker", _worker_must_not_run
    )
    registry = build_tool_registry(mcp_tools=connected_tools)
    state = {
        "pending_tool_call": {
            "name": "fake__add",
            "args": {"a": 3, "b": 5},
            "call_id": "call_1",
            "approved": True,
        },
        "messages": [],
    }
    result = await execute_tool_node(
        state,
        {"configurable": {"tool_registry": registry, "user_config": UserConfig()}},
    )
    assert result["pending_tool_call"] is None
    assert result["messages"][0]["content"].strip() == "8"


async def test_dead_mcp_tool_fails_gracefully_as_tool_result(mcp_registry):
    """Kill the server mid-session, then call through the converted Tool:
    the call returns a clear error string, it does not raise into the graph."""
    from aether_engine.tools.registry import build_tool_registry

    url, stop = start_fake_http_server()
    try:
        mcp_registry.add_server(
            "fake", {"transport": "streamable_http", "url": url, "timeout": 10}
        )
        assert (await mcp_registry.connect_server("fake"))["ok"] is True
        tools = mcp_registry.get_mcp_tools()
        assert [t.name for t in tools] == ["fake__add"]

        stop()  # kill the server mid-session
        registry = build_tool_registry(mcp_tools=tools)
        result = await registry.execute("fake__add", {"a": 1, "b": 2}, approved=True)
        assert "fake MCP is unavailable" in result
        assert "add" in result
    finally:
        try:
            await mcp_registry.disconnect_server("fake")
        except Exception:  # noqa: BLE001, S110 — best-effort cleanup in finally; the server is already dead, so disconnect may fail and must not mask the result
            pass
