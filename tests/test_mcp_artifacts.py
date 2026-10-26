import pytest
import os
from aether_engine.artifacts.manager import save_artifact, get_task_artifact_dir
from aether_engine.mcp.registry import MCPRegistry
from aether_engine.mcp.fallback import execute_mcp_tool_async

def test_artifact_manager(tmp_path, monkeypatch):
    import aether_engine.artifacts.manager as am
    monkeypatch.setattr(am, "_DEFAULT_ARTIFACTS_DIR", tmp_path)
    
    task_id = "test_task_123"
    
    save_artifact(task_id, "test_node", "test_file.txt", "Hello World")
    
    file_path = get_task_artifact_dir(task_id) / "test_node" / "test_file.txt"
    assert file_path.exists()
    assert file_path.read_text(encoding="utf-8") == "Hello World"

def test_mcp_registry():
    registry = MCPRegistry()
    registry.add_server("test_server", {"command": "echo"})
    
    servers = registry.list_servers()
    assert "test_server" in servers
    
    registry.remove_server_sync("test_server")
    assert "test_server" not in registry.list_servers()

async def test_mcp_fallback():
    # A real failure (no client connected) returns an error message rather
    # than throwing an exception — and it is a real message, not the old
    # simulated connection failure. (Issue 010 N5: the sync entry point
    # is deleted; this now exercises the async path with no client.)
    result = await execute_mcp_tool_async("test_server", "test_tool", {"arg1": "val"}, client=None)
    assert "test_server MCP is unavailable" in result
    assert "test_tool" in result
    assert "Simulated" not in result
