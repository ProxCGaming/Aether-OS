"""LangGraph node: pause for human-in-the-loop approval before executing risky tools.

ADR 0009 §4: "We favor fail-closed validation and explicit user consent for
mutation-oriented operations, even when that introduces extra latency."

If no approval_handler is configured, the tool call is **rejected** (fail-closed),
not silently approved. This is logged distinctly as APPROVAL_HANDLER_MISSING.
"""
import logging
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.nodes.pause")


async def dummy_pause_node(state: AetherState, config: RunnableConfig) -> dict:
    """A node that waits on the external approval_handler before proceeding."""
    pending = state.get("pending_tool_call")
    if not pending:
        return {}

    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or f"call_{tool_name}"

    handler = config.get("configurable", {}).get("approval_handler")
    req_id = config.get("configurable", {}).get("request_id")

    if not handler:
        # ── Fail-closed: reject when approval handler is missing (ADR 0009 §4) ──
        logger.warning(
            f"No approval_handler configured — rejecting {tool_name} (fail-closed). "
            "This indicates a configuration problem."
        )
        # Log distinctly so this is distinguishable from a normal user rejection
        try:
            from aether_engine.audit import AuditLogger
            audit = AuditLogger()
            audit.log_event("APPROVAL_HANDLER_MISSING", {
                "tool_name": tool_name,
                "args": args,
                "request_id": req_id,
                "action": "rejected_fail_closed",
            })
        except Exception:
            pass  # Don't let audit failure block the rejection

        msg = {
            "role": "tool",
            "name": tool_name,
            "content": "Error: User rejected the execution of this tool.",
            "tool_call_id": call_id,
        }
        return {
            "pending_tool_call": None,
            "messages": [msg],
        }

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
