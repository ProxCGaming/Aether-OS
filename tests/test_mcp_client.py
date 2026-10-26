"""Issue 001: raw MCP SDK client — Streamable HTTP only.

TDD red first: these tests fail until aether_engine/mcp/client.py exists.
The fake HTTP server (tests/fake_mcp_http.py) exposes one tool, ``add``.
"""
import os

import pytest

from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_connect_lists_add_tool_and_calls_it(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    assert manager.get_status("fake")["status"] == "disconnected"
    result = await manager.connect("fake", config)
    assert result["ok"] is True, result
    assert manager.get_status("fake")["status"] == "connected"

    tools = await manager.list_tools("fake")
    assert [t.name for t in tools] == ["add"]

    out = await manager.call_tool("fake", "add", {"a": 3, "b": 5})
    assert out.strip() == "8"

    await manager.disconnect("fake")
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_test_connection_returns_tools_without_leaving_session(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    result = await manager.test_connection("fake", config)
    assert result["ok"] is True
    assert result["tools"] == ["add"]
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_call_on_disconnected_server_fails_gracefully(manager):
    with pytest.raises(Exception) as ctx:
        await manager.call_tool("ghost", "add", {"a": 1, "b": 2})
    assert "ghost" in str(ctx.value)


async def test_dead_server_fails_immediately_no_retry(manager):
    config = {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:9/mcp",  # nothing listens here
        "timeout": 2,
    }
    result = await manager.connect("dead", config)
    assert result["ok"] is False
    assert result["error"]
    assert manager.get_status("dead")["status"] == "failed"


async def test_stdio_transport_rejected_in_issue_001(manager):
    # stdio is issue 003 — in 001 the client must refuse it clearly, not crash.
    config = {"transport": "stdio", "command": "echo", "args": [], "timeout": 2}
    result = await manager.connect("local", config)
    assert result["ok"] is False
    assert "stdio" in result["error"].lower()


def test_header_env_var_expansion():
    from aether_engine.mcp.client import expand_env_vars

    os.environ["AETHER_TEST_KEY"] = "secret-123"
    assert expand_env_vars("Bearer ${AETHER_TEST_KEY}") == "Bearer secret-123"
    assert expand_env_vars("${MISSING_VAR:-fallback}") == "fallback"
    assert expand_env_vars("plain") == "plain"
