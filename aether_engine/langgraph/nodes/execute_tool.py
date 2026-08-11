import json
import asyncio
import logging
from typing import Dict, Any

from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.tools.registry import ToolExecutionError, ToolNotFoundError

logger = logging.getLogger("aether_engine.langgraph.nodes.execute_tool")

async def execute_tool_node(state: AetherState, config: RunnableConfig) -> dict:
    """Executes a tool call pending in the state using the provided ToolRegistry."""
    pending = state.get("pending_tool_call")
    if not pending:
        logger.warning("execute_tool_node called but no pending tool call in state.")
        return {"messages": []}

    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or pending.get("id") or f"call_{tool_name}"

    registry = config.get("configurable", {}).get("tool_registry")
    if not registry:
        result_text = f"Error: No tool registry provided to graph. Cannot execute {tool_name}."
    else:
        try:
            logger.info(f"Executing tool {tool_name} with args {args}")
            # The registry's get_tool returns the Tool definition, and execute_fn handles it
            tool = registry.get_tool(tool_name)
            result = await tool.execute_fn(**args) if asyncio.iscoroutinefunction(tool.execute_fn) else tool.execute_fn(**args)
            
            if not isinstance(result, str):
                result_text = json.dumps(result)
            else:
                result_text = result
        except ToolNotFoundError:
            result_text = f"Error: Tool {tool_name} not found in registry."
        except Exception as e:
            logger.error(f"Error executing {tool_name}: {e}")
            result_text = f"Error executing tool {tool_name}: {e}"

    tool_message = {
        "role": "tool",
        "name": tool_name,
        "content": result_text,
        "tool_call_id": call_id
    }

    # Clear pending_tool_call and add the message
    return {
        "pending_tool_call": None,
        "messages": [tool_message]
    }
