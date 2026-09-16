import logging
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.nodes.pause")

def dummy_pause_node(state: AetherState, config: RunnableConfig) -> dict:
    """A node that uses LangGraph native interrupt for human-in-the-loop approval."""
    pending = state.get("pending_tool_call")
    if not pending:
        return {}

    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or f"call_{tool_name}"
    req_id = config.get("configurable", {}).get("request_id")

    logger.info(f"Interrupting graph for approval of {tool_name}")
    
    # Pause execution and save state to sqlite
    decision = interrupt({
        "type": "approval_request",
        "tool_name": tool_name,
        "args": args,
        "request_id": req_id,
        "task_class": config.get("configurable", {}).get("task_class", "standard")
    })

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
