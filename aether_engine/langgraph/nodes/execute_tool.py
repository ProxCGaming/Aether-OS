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
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from aether_common.contracts import Event, EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.state import TOOL_LOOP_GUARD_THRESHOLD, AetherState
from aether_engine.tools.approval_cache import get_default_cache
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
            # Epic 8, E8-2.1: Pre-capture content for edit_file to compute unified diff
            old_file_content = None
            if tool_name == "edit_file" and args.get("path"):
                try:
                    p = Path(args["path"])
                    if p.is_file():
                        old_file_content = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    old_file_content = None

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
                approval_mode=user_config.approval_mode if user_config else "ask",
                approval_cache=get_default_cache(),
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

            # Epic 8, E8-2.1: Auto-collect file writes and edits into ArtifactStore
            if executed_write and tool_name in ("write_file", "edit_file"):
                try:
                    from aether_common.text_edit import make_unified_diff
                    from aether_engine.artifacts.store import get_artifact_store

                    art_store = get_artifact_store()
                    file_path = args.get("path")
                    file_p = Path(file_path) if file_path else None
                    title = file_p.name if file_p else "file"
                    sess_id = state.get("session_id") or configurable.get("session_id") or "default"
                    t_id = state.get("task_id") or configurable.get("request_id") or "default"

                    diff_text = None
                    if tool_name == "edit_file":
                        kind = "diff"
                        try:
                            new_file_content = file_p.read_text(encoding="utf-8", errors="replace") if (file_p and file_p.is_file()) else ""
                            diff_text = make_unified_diff(old_file_content or "", new_file_content, str(file_path))
                        except Exception as diff_exc:  # noqa: BLE001 — diff generation error is safely displayed
                            diff_text = f"Diff unavailable: {diff_exc}"
                    else:
                        kind = "file"

                    artifact_record = art_store.record_artifact(
                        session_id=sess_id,
                        task_id=t_id,
                        kind=kind,
                        title=title,
                        file_path=str(file_path) if file_path else None,
                        content=diff_text,
                        metadata={"tool": tool_name},
                    )

                    if t_id:
                        event_bus.publish(
                            t_id,
                            Event(
                                type=EventType.ARTIFACT_RECORDED,
                                request_id=t_id,
                                payload={
                                    "artifact_id": artifact_record["id"],
                                    "session_id": artifact_record["session_id"],
                                    "task_id": artifact_record["task_id"],
                                    "kind": artifact_record["kind"],
                                    "title": artifact_record["title"],
                                    "path": artifact_record["file_path"],
                                    "created_at": artifact_record["created_at"],
                                },
                            ),
                        )

                    # Epic 8, E8-4.2: Auto-register agent-generated media into MediaStore
                    if file_p:
                        ext = file_p.suffix.lower()
                        is_img = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
                        is_vid = ext in (".mp4", ".webm", ".mkv", ".mov", ".avi")
                        if is_img or is_vid:
                            try:
                                from aether_engine.media.store import get_media_store
                                m_store = get_media_store()
                                m_type = "video" if is_vid else "image"
                                med_item = m_store.register_media(
                                    file_path=str(file_path),
                                    collection="generated",
                                    media_type=m_type,
                                    session_id=sess_id,
                                    message_id=None,
                                    probe_metadata={"origin": "agent_generated", "tool": tool_name},
                                )
                                if t_id:
                                    event_bus.publish(
                                        t_id,
                                        Event(
                                            type=EventType.MEDIA_RECORDED,
                                            request_id=t_id,
                                            payload=med_item,
                                        ),
                                    )
                                # Epic 8, E8-4.3: analyse on arrival —
                                # detached, never blocks the tool call.
                                try:
                                    from aether_engine.media.analyzer import (
                                        schedule_media_analysis,
                                    )

                                    schedule_media_analysis(med_item["id"])
                                except Exception:  # noqa: BLE001, S110
                                    pass
                            except Exception:
                                logger.exception("Failed to auto-register generated media into MediaStore")
                except Exception:
                    logger.exception(f"Failed to record artifact for {tool_name}")

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
    # Epic 8, E8-1.3: an approval-time argument edit is marked on the
    # tool message so the executor's TOOL_ACTIVITY payload (and thus
    # the tool card's "edited by you" pill) can reflect it. The args
    # above are the ones that actually ran through the pipeline.
    if pending.get("edited"):
        tool_message["edited"] = True

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
