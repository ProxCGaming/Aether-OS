"""Issue 003: stdio transport — local MCP servers end to end.

TDD red first: these tests fail until aether_engine/mcp/client.py gains
its stdio branch (in 001 stdio was refused with a clear error). Tests use
the fake stdio fixture (tests/fake_stdio_server.py), never a real ``npx``
install — the real filesystem server is the human QA demo.
"""
import os
import sys
from pathlib import Path

import pytest

from aether_engine.mcp.client import MCPClientError

FAKE_STDIO = Path(__file__).parent / "fake_stdio_server.py"


def stdio_config(**overrides):
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_STDIO)],
        "timeout": 10,
    }
    config.update(overrides)
    return config


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_stdio_connect_lists_add_and_calls_it(manager):
    assert manager.get_status("fake")["status"] == "disconnected"
    result = await manager.connect("fake", stdio_config())
    assert result["ok"] is True, result
    assert manager.get_status("fake")["status"] == "connected"

    tools = await manager.list_tools("fake")
    assert sorted(t.name for t in tools) == ["add", "crash", "echo_env", "pwd"]

    out = await manager.call_tool("fake", "add", {"a": 3, "b": 5})
    assert out.strip() == "8"

    await manager.disconnect("fake")
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_stdio_test_connection_returns_tools_without_leaving_session(manager):
    result = await manager.test_connection("fake", stdio_config())
    assert result["ok"] is True
    assert sorted(result["tools"]) == ["add", "crash", "echo_env", "pwd"]
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_stdio_env_and_cwd_reach_the_subprocess(manager, tmp_path):
    config = stdio_config(
        env={"AETHER_STDIO_MARKER": "hello-stdio", "AETHER_STDIO_NUM": "${AETHER_STDIO_NUM:-42}"},
        cwd=str(tmp_path),
    )
    assert (await manager.connect("fake", config))["ok"] is True
    assert (await manager.call_tool("fake", "echo_env", {"name": "AETHER_STDIO_MARKER"})).strip() == "hello-stdio"
    # ${VAR:-default} expansion applies to env values too, not just headers.
    assert (await manager.call_tool("fake", "echo_env", {"name": "AETHER_STDIO_NUM"})).strip() == "42"
    assert (await manager.call_tool("fake", "pwd", {})).strip() == str(tmp_path)
    await manager.disconnect("fake")


async def test_stdio_server_exiting_immediately_fails_gracefully(manager):
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(1)"],
        "timeout": 5,
    }
    result = await manager.connect("dead", config)
    assert result["ok"] is False
    assert result["error"]
    assert manager.get_status("dead")["status"] == "failed"


async def test_stdio_server_dying_mid_session_marks_failed_no_retry(manager):
    """Kill the server mid-session: the call fails gracefully, the server
    is marked failed (not retried), disconnect does not raise, and a
    manual reconnect starts a fresh process."""
    assert (await manager.connect("fake", stdio_config()))["ok"] is True
    with pytest.raises(MCPClientError):
        await manager.call_tool("fake", "crash", {})
    assert manager.get_status("fake")["status"] == "failed"
    # A further call must not silently reconnect / retry.
    with pytest.raises(MCPClientError):
        await manager.call_tool("fake", "add", {"a": 1, "b": 2})
    await manager.disconnect("fake")
    assert manager.get_status("fake")["status"] == "disconnected"
    # Manual reconnect works: a fresh subprocess is spawned.
    assert (await manager.connect("fake", stdio_config()))["ok"] is True
    assert (await manager.call_tool("fake", "add", {"a": 2, "b": 3})).strip() == "5"
    await manager.disconnect("fake")


async def test_stdio_registry_exposes_prefixed_tool(tmp_path, monkeypatch):
    """Registry level: a stdio server's tools appear as ``server__tool``
    and run through the registry's converted Tool (EXECUTE, in-process)."""
    import aether_engine.mcp.registry as reg_module
    from aether_engine.mcp.registry import MCPRegistry

    monkeypatch.setattr(reg_module, "_MCP_CONFIG_PATH", tmp_path / "mcp_servers.json")
    registry = MCPRegistry()
    registry.add_server("local", stdio_config())
    assert (await registry.connect_server("local"))["ok"] is True

    tools = registry.get_mcp_tools()
    assert sorted(t.name for t in tools) == [
        "local__add", "local__crash", "local__echo_env", "local__pwd",
    ]
    add_tool = next(t for t in tools if t.name == "local__add")
    assert add_tool.source == "mcp:local"
    assert (await add_tool.execute_fn(a=3, b=5)).strip() == "8"

    await registry.disconnect_server("local")
    assert registry.get_mcp_tools() == []


def test_stdio_config_validation_rejects_missing_command():
    from aether_engine.mcp.registry import MCPConfigError, validate_mcp_server_config

    with pytest.raises(MCPConfigError, match="requires a 'command'"):
        validate_mcp_server_config("x", {"transport": "stdio", "command": ""})
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config("x", {"transport": "stdio", "args": ["a"]})
    cfg = validate_mcp_server_config(
        "fs",
        {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/ws"],
            "env": {"KEY": "v"},
            "cwd": "/tmp",
        },
    )
    assert cfg["transport"] == "stdio"
    assert cfg["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/ws"]
    assert cfg["cwd"] == "/tmp"
