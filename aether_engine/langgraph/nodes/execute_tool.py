"""LangGraph node: execute a pending tool call via the single trust pipeline.

Phase 10.2, C1: this node is a thin adapter over ``ToolRegistry.execute()``
— the Phase 9 hardened pipeline (policy → pre-flight → sandbox, ADR 0021 /
ADR 0009 / ADR 0010). The node used to duplicate that enforcement inline
(policy check, ``validate_tool_request``, direct ``execute_tool_in_worker``
call with no audit logger), so fixes to the audited pipeline never reached
production traffic and the live path produced no worker audit events.
That duplication is deleted: every production tool execution now goes
through ``ToolRegistry.execute()`` and produces the same audit events
(``TOOL_BLOCKED_BY_POLICY``, ``WORKER_EXECUTION_COMPLETED`` /
``WORKER_EXECUTION_TERMINATED`` / ``WORKER_EXECUTION_TIMEOUT`` /
``WORKER_EXECUTION_REFUSED``).

What the node still owns (not enforcement, so not duplicated):
  * the per-specialist tool allow-list (``agent_tools``) — role scoping
    the registry knows nothing about;
  * translating ``ToolRegistry`` exceptions back into the tool-message
    shape (``{"role": "tool", "content": ...}``) the graph expects,
    preserving the exact denial/approval/validation wording callers
    already assert on.

See ADR 0009 (fail-closed, pre-flight), ADR 0010 (workspace restriction), ADR 0011 §2 (preservation).
"""
import json
import logging
from typing import Dict, Any

from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.tools.registry import ToolExecutionError, ToolNotFoundError

logger = logging.getLogger("aether_engine.langgraph.nodes.execute_tool")


async def execute_tool_node(state: AetherState, config: RunnableConfig) -> dict:
    """Executes a tool call pending in the state using the provided ToolRegistry.

    Dispatches through ``ToolRegistry.execute()`` — the single pipeline:
      1. Policy check (ADR 0021) — ``deny`` / unapproved ``ask`` fail closed.
      2. Pre-flight validation (ADR 0009 §2) — failures fail closed.
      3. Sandbox routing (ADR 0010) — non-READ_ONLY tools run in the
         ``aether_worker`` subprocess; READ_ONLY tools run in-process.
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

    # Check if tool is allowed for the active specialist
    active_agent = state.get("active_specialist")
    agent_tools = configurable.get("agent_tools", {})
    if active_agent and active_agent in agent_tools:
        allowed_tools = agent_tools[active_agent]
        if allowed_tools is not None and isinstance(allowed_tools, list):
            if tool_name not in allowed_tools:
                error_msg = f"Error: Tool '{tool_name}' is currently disabled for your agent role. You are not allowed to execute it."
                logger.warning(f"Agent '{active_agent}' attempted to use disabled tool '{tool_name}'.")
                return {
                    "messages": [{
                        "role": "tool",
                        "name": tool_name,
                        "content": error_msg,
                        "tool_call_id": call_id,
                    }],
                    "pending_tool_call": None
                }

    if not registry:
        result_text = f"Error: No tool registry provided to graph. Cannot execute {tool_name}."
    else:
        # Single pipeline (Phase 10.2, C1): policy → pre-flight → sandbox,
        # with the worker audit events, all inside ToolRegistry.execute().
        # The registry raises on denial / validation failure; translate
        # those back into the tool-message wording this node has always
        # returned so callers see the same denial/approval/validation
        # strings as before the unification.
        user_config = configurable.get("user_config")
        audit_logger = configurable.get("audit_logger")
        try:
            result = await registry.execute(
                tool_name,
                args,
                workspace_roots=workspace_roots,
                task_class=task_class,
                policies=user_config.tool_policies if user_config else {},
                approved=bool(pending.get("approved")),
                audit_logger=audit_logger,
            )
            result_text = json.dumps(result) if not isinstance(result, str) else (result or f"Tool {tool_name} completed.")
        except ToolNotFoundError:
            result_text = f"Error: Tool {tool_name} not found in registry."
        except ToolExecutionError as e:
            msg = str(e)
            if "blocked by policy (deny)" in msg:
                result_text = f"Error: Tool '{tool_name}' is blocked by user policy (Deny)."
                logger.warning(result_text)
            elif "requires approval (policy=ask)" in msg:
                result_text = f"Error: Tool '{tool_name}' requires approval but was bypassed."
                logger.warning(result_text)
            elif msg.startswith("Pre-flight validation failed"):
                # Registry message: "Pre-flight validation failed for '<name>': <error>"
                detail = msg.split(": ", 1)[-1] if ": " in msg else msg
                result_text = f"Validation failed: {detail}"
                logger.warning(f"Pre-flight validation failed for {tool_name}: {detail}")
            else:
                logger.error(f"Error executing {tool_name}: {e}")
                result_text = f"Error executing tool {tool_name}: {e}"
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
