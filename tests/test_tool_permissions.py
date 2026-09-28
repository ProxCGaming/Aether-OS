import pytest
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from langchain_core.runnables import RunnableConfig

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
