import logging

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.nodes.pause")


def _edit_preview(args: dict, workspace_roots: list) -> dict:
    """Best-effort diff preview for an edit_file approval (Epic 2, E2-03).

    Computed at pause time from the file as it stands NOW. Any failure
    (stale hash, bad anchor, containment) becomes a preview error the
    card shows — the preview must never break the pause itself, and
    execution re-verifies everything in the worker regardless.
    """
    try:
        from aether_common.text_edit import (
            apply_text_edits,
            content_hash,
            make_unified_diff,
        )
        from aether_engine.tools.paths import resolve_contained

        target = resolve_contained(str(args.get("path", "")), workspace_roots)
        data = target.read_bytes()
        expected = args.get("expected_hash") or ""
        if expected and content_hash(data) != expected:
            return {"error": "The file changed since the agent read it — this edit will be refused at execution unless the agent re-reads the file."}
        old_text = data.decode("utf-8")
        new_text = apply_text_edits(old_text, args.get("edits") or [])
        return {"diff": make_unified_diff(old_text, new_text, str(args.get("path")))}
    except Exception as exc:  # noqa: BLE001 — preview boundary: a failed preview degrades to an error note, never a broken approval pause.
        return {"error": f"Preview unavailable: {exc}"}

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
    interrupt_payload = {
        "type": "approval_request",
        "tool_name": tool_name,
        "args": args,
        "request_id": req_id,
        "task_class": config.get("configurable", {}).get("task_class", "standard")
    }
    if tool_name == "edit_file":
        roots = config.get("configurable", {}).get("workspace_roots") or []
        interrupt_payload["preview"] = _edit_preview(args, roots)
    decision = interrupt(interrupt_payload)

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

    # If approved, mutate the pending_tool_call to reflect approval, and graph proceeds to execute_tool
    return {
        "pending_tool_call": {**pending, "approved": True}
    }
