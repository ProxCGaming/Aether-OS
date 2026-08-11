import logging
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.nodes.pause")

async def dummy_pause_node(state: AetherState, config: RunnableConfig) -> dict:
    """A node that waits on the external approval_handler before proceeding."""
    pending = state.get("pending_tool_call")
    if not pending:
        return {}
    
    handler = config.get("configurable", {}).get("approval_handler")
    req_id = config.get("configurable", {}).get("request_id")
    
    if not handler:
        # If no handler is provided, we auto-approve or fail. Here we'll auto-approve.
        return {}
        
    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or f"call_{tool_name}"
    
    logger.info(f"Pausing for approval of {tool_name}")
    decision = await handler(tool_name, args, req_id)
    
    if not decision:
        logger.info(f"User rejected {tool_name}")
        # Clear the tool call and return a rejection message
        msg = {
            "role": "tool",
            "name": tool_name,
            "content": "Error: User rejected the execution of this tool.",
            "tool_call_id": call_id
        }
        return {
            "pending_tool_call": None,
            "messages": [msg]
        }
    
    # If approved, we do nothing and the graph proceeds to execute_tool!
    return {}
