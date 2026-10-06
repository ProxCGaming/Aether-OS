import pytest
from unittest.mock import patch
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from langchain_core.runnables import RunnableConfig
from aether_engine.tools.policy import resolve_tool_policy, resolve_policy_by_name
from aether_engine.tools.registry import Tool, ToolRisk, ToolRegistry
from aether_engine.config import UserConfig
from aether_engine.langgraph.graph import tool_router

@pytest.mark.asyncio
async def test_tool_permission_enforcement():
    # Setup state with a pending tool call and active specialist
    state = {
        "pending_tool_call": {
            "name": "get_current_time",
            "call_id": "call_123",
            "args": {}
        },
        "active_specialist": "coder"
    }

    # Case 1: Tool is disabled for the coder
    config: RunnableConfig = {
        "configurable": {
            "agent_tools": {
                "coder": ["write_file", "execute_shell"]  # get_current_time is not here
            }
        }
    }
    
    result = await execute_tool_node(state, config)
    
    assert "messages" in result
    assert result["pending_tool_call"] is None
    msg = result["messages"][0]
    assert msg["role"] == "tool"
    assert msg["name"] == "get_current_time"
    assert "disabled for your agent role" in msg["content"]

@pytest.mark.parametrize("risk, setting, expected_policy", [
    (ToolRisk.READ_ONLY, "Always Allow", "allow"),
    (ToolRisk.READ_ONLY, "Require Approval", "ask"),
    (ToolRisk.READ_ONLY, "Deny", "deny"),
    (ToolRisk.READ_ONLY, None, "allow"),
    (ToolRisk.WRITE, "Always Allow", "ask"),  # Always Allow ignored for risky
    (ToolRisk.WRITE, "Require Approval", "ask"),
    (ToolRisk.WRITE, "Deny", "deny"),
    (ToolRisk.WRITE, None, "ask"),
    (ToolRisk.EXECUTE, None, "ask"),
    (ToolRisk.DESTRUCTIVE, None, "ask"),
])
def test_resolve_tool_policy_matrix(risk, setting, expected_policy):
    tool = Tool(name="test_tool", description="", risk=risk)
    policies = {"test_tool": setting} if setting else {}
    assert resolve_tool_policy(tool, policies) == expected_policy

def test_resolve_policy_by_name_unknown():
    # Unknown tool fails closed to ask
    assert resolve_policy_by_name("unknown_tool", {}) == "ask"

@pytest.mark.asyncio
async def test_layer3_bypass_deny_and_ask():
    # Setup state with pending tool call
    state_deny = {"pending_tool_call": {"name": "test_deny", "call_id": "1"}, "active_specialist": "coder"}
    state_ask = {"pending_tool_call": {"name": "test_ask", "call_id": "2"}, "active_specialist": "coder"}
    
    registry = ToolRegistry()
    registry.register(Tool(name="test_deny", description="", risk=ToolRisk.READ_ONLY))
    registry.register(Tool(name="test_ask", description="", risk=ToolRisk.WRITE))

    config: RunnableConfig = {
        "configurable": {
            "tool_registry": registry,
            "user_config": UserConfig(tool_policies={"test_deny": "Deny", "test_ask": "Require Approval"})
        }
    }
    
    # 1. Deny should be blocked
    res_deny = await execute_tool_node(state_deny, config)
    assert "blocked by user policy" in res_deny["messages"][0]["content"]
    assert res_deny["pending_tool_call"] is None
    
    # 2. Ask without approval should be blocked
    res_ask = await execute_tool_node(state_ask, config)
    assert "requires approval but was bypassed" in res_ask["messages"][0]["content"]
    assert res_ask["pending_tool_call"] is None

def test_layer2_routing():
    registry = ToolRegistry()
    registry.register(Tool(name="test_read", description="", risk=ToolRisk.READ_ONLY))
    
    # Explicit Require Approval on a read-only tool
    config = {
        "configurable": {
            "tool_registry": registry,
            "user_config": UserConfig(tool_policies={"test_read": "Require Approval"})
        }
    }
    
    state = {"pending_tool_call": {"name": "test_read", "args": {}}}
    route = tool_router(state, config)
    assert route == "pause_for_approval"

@pytest.mark.asyncio
@patch("aether_engine.workers.sandbox.execute_tool_in_worker")
async def test_sandbox_routing_by_risk(mock_worker):
    registry = ToolRegistry()
    # A custom risky tool
    registry.register(Tool(name="custom_risky", description="", risk=ToolRisk.WRITE))
    
    # Needs approval
    state = {"pending_tool_call": {"name": "custom_risky", "call_id": "3", "approved": True}}
    config = {
        "configurable": {
            "tool_registry": registry,
            "user_config": UserConfig()
        }
    }
    
    # Worker returns a string
    mock_worker.return_value = "sandbox success"
    
    res = await execute_tool_node(state, config)
    assert mock_worker.called
    assert "sandbox success" in res["messages"][0]["content"]
