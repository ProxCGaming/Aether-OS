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

from langchain_core.runnables import RunnableConfig

from aether_common.contracts import Event, EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.state import TOOL_LOOP_GUARD_THRESHOLD, AetherState
from aether_engine.tools.registry import ToolExecutionError, ToolNotFoundError
from aether_engine.tools.scratchpad import (
    SCRATCHPAD_TOOL_NAME,
    scratchpad_event_payload,
)

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
    allowed_tools = agent_tools.get(active_agent) if active_agent else None
    if isinstance(allowed_tools, list) and tool_name not in allowed_tools:
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

    # ── Plan-mode backstop (Epic 1, E1-09) ────────────────────────────
    # Specialists in plan mode are never offered non-READ_ONLY tools,
    # so a risky pending call here means forged or stale state. Refuse
    # it centrally rather than trusting that every caller filtered.
    if state.get("plan_mode") and registry:
        try:
            tool_def = registry.get_tool(tool_name)
        except ToolNotFoundError:
            tool_def = None
        if tool_def is not None and tool_def.risk.value != "read_only":
            error_msg = f"Error: Tool '{tool_name}' is not available in plan mode (read-only). Switch plan mode off to execute it."
            logger.warning(f"Plan mode blocked {tool_name} (risk={tool_def.risk.value}).")
            return {
                "messages": [{
                    "role": "tool",
                    "name": tool_name,
                    "content": error_msg,
                    "tool_call_id": call_id,
                }],
                "pending_tool_call": None
            }

    # ── Loop guard (Epic 1, E1-03) ────────────────────────────────────
    # Count consecutive identical calls (same tool, same arguments).
    # At the threshold, do not execute again: hand the specialist a
    # corrective steer as the tool result and reset the counter, so a
    # stuck retry loop stops burning tokens. The graph recursion limit
    # remains the hard bound behind this.
    signature = f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"
    if state.get("tool_call_signature") == signature:
        repeats = (state.get("tool_call_repeats") or 0) + 1
    else:
        repeats = 1
    if repeats >= TOOL_LOOP_GUARD_THRESHOLD:
        steer = (
            f"Loop guard: you have called '{tool_name}' with identical arguments "
            f"{repeats} times in a row and it has been stopped. Do not retry the "
            "same call. Change your approach: use different arguments, a different "
            "tool, or explain to the user what is blocking you."
        )
        logger.warning(f"Loop guard triggered for {tool_name} after {repeats} identical calls.")
        return {
            "messages": [{
                "role": "tool",
                "name": tool_name,
                "content": steer,
                "tool_call_id": call_id,
            }],
            "pending_tool_call": None,
            "tool_call_signature": None,
            "tool_call_repeats": 0,
        }

    executed_write = False
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
                path_rules=user_config.path_rules if user_config else None,
                shell_extras=(
                    {"deny": user_config.extra_shell_deny_patterns, "ask": user_config.extra_shell_ask_patterns}
                    if user_config else None
                ),
                approved=bool(pending.get("approved")),
                audit_logger=audit_logger,
            )
            result_text = json.dumps(result) if not isinstance(result, str) else (result or f"Tool {tool_name} completed.")
            # E1-13 prewalk marker: only a tool that actually executed
            # counts, and only if it can change something.
            try:
                executed_write = registry.get_tool(tool_name).risk.value != "read_only"
            except Exception:  # noqa: BLE001 — unknown tool metadata must not fail the run; it just doesn't flip the prewalk switch.
                executed_write = False
            # E1-17: publish scratchpad renders for the UI panel
            # (sanitised in scratchpad_event_payload; display-only).
            if tool_name == SCRATCHPAD_TOOL_NAME:
                payload = scratchpad_event_payload(args, active_agent)
                task_id = state.get("task_id")
                if payload and task_id:
                    payload["task_id"] = task_id
                    event_bus.publish(task_id, Event(
                        type=EventType.SCRATCHPAD, request_id="", payload=payload))
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
        except Exception as e:  # noqa: BLE001 — deliberate boundary: any registry failure becomes a tool message, never a crashed graph run; the error is logged and surfaced verbatim above.
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
    update = {
        "pending_tool_call": None,
        "messages": [tool_message],
        "tool_call_signature": signature,
        "tool_call_repeats": repeats,
    }
    if executed_write:
        update["write_tool_used"] = True
    return update
