"""LangGraph node: execute a pending tool call with pre-flight validation and sandbox routing.

Safe tools (read_file, web_search, get_current_time, summarize) run in-process via the registry.
Risky tools (write_file, delete_file, execute_shell) are validated via pre_flight.py then
executed in an isolated aether_worker subprocess via the shared sandbox module.

See ADR 0009 (fail-closed, pre-flight), ADR 0010 (workspace restriction), ADR 0011 §2 (preservation).
"""
import json
import asyncio
import logging
from typing import Dict, Any

from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.tools.registry import ToolExecutionError, ToolNotFoundError
from aether_engine.validation.pre_flight import validate_tool_request

logger = logging.getLogger("aether_engine.langgraph.nodes.execute_tool")

# Tools that require sandbox execution (same classification as the pause conditional edge in graph.py)
RISKY_TOOLS = {"write_file", "execute_shell", "delete_file"}


async def execute_tool_node(state: AetherState, config: RunnableConfig) -> dict:
    """Executes a tool call pending in the state using the provided ToolRegistry.

    Before dispatching any tool:
      1. Runs ``validate_tool_request()`` and rejects if validation fails (ADR 0009 fail-closed).
      2. For risky tools, routes through ``execute_tool_in_worker()`` (aether_worker subprocess).
      3. For safe tools, calls ``tool.execute_fn()`` directly.
    """
    pending = state.get("pending_tool_call")
    if not pending:
        logger.warning("execute_tool_node called but no pending tool call in state.")
        return {"messages": []}

    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or pending.get("id") or f"call_{tool_name}"

    configurable = config.get("configurable", {})
    registry = configurable.get("tool_registry")
    workspace_roots = configurable.get("workspace_roots", [])
    task_class = configurable.get("task_class", "standard")

    if not registry:
        result_text = f"Error: No tool registry provided to graph. Cannot execute {tool_name}."
    else:
        # ── Step 1: Pre-flight validation (ADR 0009 §2) ──────────────────
        validation = validate_tool_request(tool_name, args, workspace_roots=workspace_roots)
        if not validation.is_valid:
            logger.warning(f"Pre-flight validation failed for {tool_name}: {validation.error}")
            result_text = f"Validation failed: {validation.error}"
        else:
            try:
                if tool_name in RISKY_TOOLS:
                    # ── Step 2a: Risky tools → sandbox worker subprocess ──
                    logger.info(f"Routing risky tool {tool_name} through sandbox worker (task_class={task_class})")
                    from aether_engine.workers.sandbox import execute_tool_in_worker
                    result = await execute_tool_in_worker(
                        tool_name=tool_name,
                        args=args,
                        task_class=task_class,
                    )
                    result_text = json.dumps(result) if not isinstance(result, str) else (result or f"Tool {tool_name} completed.")
                else:
                    # ── Step 2b: Safe tools → direct in-process execution ──
                    logger.info(f"Executing safe tool {tool_name} with args {args}")
                    tool = registry.get_tool(tool_name)
                    result = await tool.execute_fn(**args) if asyncio.iscoroutinefunction(tool.execute_fn) else tool.execute_fn(**args)
                    result_text = json.dumps(result) if not isinstance(result, str) else result

            except ToolNotFoundError:
                result_text = f"Error: Tool {tool_name} not found in registry."
            except Exception as e:
                logger.error(f"Error executing {tool_name}: {e}")
                result_text = f"Error executing tool {tool_name}: {e}"

    tool_message = {
        "role": "tool",
        "name": tool_name,
        "args": args,
        "content": result_text,
        "tool_call_id": call_id
    }

    # Clear pending_tool_call and add the message
    return {
        "pending_tool_call": None,
        "messages": [tool_message]
    }
