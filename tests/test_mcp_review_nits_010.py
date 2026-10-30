"""Issue 010: review NITs on 001 (N1-N6).

Written red-first: each test here fails against the pre-010 code.
"""
import inspect
from types import SimpleNamespace

import pytest

from aether_engine.mcp import fallback as fallback_mod
from aether_engine.mcp.client import MCPClientManager, _make_http_client
from aether_engine.mcp.fallback import execute_mcp_tool_async
from aether_engine.mcp.registry import (
    MCPConfigError,
    MCPRegistry,
    validate_mcp_server_config,
)
from aether_engine.tools.registry import build_tool_registry


# N1: timeout 0 is rejected; the stated range is 1-300s (PRD wording fix).
def test_n1_timeout_zero_rejected_with_1_to_300_message():
    with pytest.raises(MCPConfigError, match="1-300"):
        validate_mcp_server_config("srv", {"url": "https://example.com/mcp", "timeout": 0})


def test_n1_timeout_one_accepted():
    cfg = validate_mcp_server_config("srv", {"url": "https://example.com/mcp", "timeout": 1})
    assert cfg["timeout"] == 1


def test_n1_timeout_301_rejected():
    with pytest.raises(MCPConfigError):
        validate_mcp_server_config("srv", {"url": "https://example.com/mcp", "timeout": 301})


# N2: build_tool_registry has no dead user_config parameter.
def test_n2_build_tool_registry_has_no_user_config_parameter():
    params = inspect.signature(build_tool_registry).parameters
    assert "user_config" not in params
    registry = build_tool_registry(mcp_tools=[])
    assert registry is not None


# N3: _make_http_client respects NO_PROXY for remote hosts.
async def test_n3_no_proxy_matching_host_uses_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("NO_PROXY", "example.com")
    client = _make_http_client("https://example.com/mcp", {}, 30)
    try:
        assert len(client._mounts) == 0
    finally:
        await client.aclose()


async def test_n3_no_proxy_non_matching_host_uses_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("NO_PROXY", "other.com")
    client = _make_http_client("https://example.com/mcp", {}, 30)
    try:
        assert len(client._mounts) == 1
    finally:
        await client.aclose()


async def test_n3_no_proxy_suffix_match(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("NO_PROXY", ".example.com")
    client = _make_http_client("https://api.example.com/mcp", {}, 30)
    try:
        assert len(client._mounts) == 0
    finally:
        await client.aclose()


async def test_n3_malformed_no_proxy_entry_does_not_break(monkeypatch):
    # The old [::1] NO_PROXY bug: a malformed entry must never break a request.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("NO_PROXY", "[::1], example.com")
    client = _make_http_client("https://example.com/mcp", {}, 30)
    try:
        assert len(client._mounts) == 0
    finally:
        await client.aclose()


# N4: output truncation uses tool.max_output_chars, not a hardcoded 20000.
class _LongResultClient:
    async def call_tool(self, server, tool, args, timeout=None):
        return "x" * 100


async def test_n4_truncation_uses_tool_max_output_chars(tmp_path):
    registry = MCPRegistry(config_path=tmp_path / "mcp_servers.json", client=_LongResultClient())
    tool = registry._convert_tool(
        "srv", SimpleNamespace(name="add", description="add", inputSchema={"type": "object"})
    )
    tool.max_output_chars = 10
    result = await tool.execute_fn(a=1)
    assert result.startswith("x" * 10)
    assert "10 chars" in result


# N5: the sync entry point is gone; the async path handles no-client gracefully.
def test_n5_sync_entry_point_deleted():
    assert not hasattr(fallback_mod, "execute_mcp_tool_with_fallback")


async def test_n5_async_path_with_no_client_returns_unavailable():
    result = await execute_mcp_tool_async("test_server", "test_tool", {}, client=None)
    assert "test_server MCP is unavailable" in result
    assert "test_tool" in result
    assert "Simulated" not in result


# N6: test_connection's stack close must not mask the test result.
class _RaisingCloseStack:
    async def aclose(self):
        raise RuntimeError("close exploded")


class _FakeSession:
    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="add")])


async def test_n6_test_connection_close_failure_does_not_mask_success(monkeypatch):
    manager = MCPClientManager()

    async def _fake_open(config):
        return _RaisingCloseStack(), _FakeSession()

    monkeypatch.setattr(manager, "_open_session", _fake_open)
    result = await manager.test_connection("srv", {"url": "https://example.com/mcp"})
    assert result == {"ok": True, "error": None, "tools": ["add"]}


async def test_n6_test_connection_close_failure_does_not_mask_list_failure(monkeypatch):
    manager = MCPClientManager()

    class _FailingSession:
        async def list_tools(self):
            raise RuntimeError("list exploded")

    async def _fake_open(config):
        return _RaisingCloseStack(), _FailingSession()

    monkeypatch.setattr(manager, "_open_session", _fake_open)
    result = await manager.test_connection("srv", {"url": "https://example.com/mcp"})
    assert result["ok"] is False
    assert "list exploded" in result["error"]
