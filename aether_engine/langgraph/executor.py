import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

try:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    from aether_engine.langgraph.graph import GRAPH_RECURSION_LIMIT, compile_graph
    from aether_engine.langgraph.state import AetherState
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    RunnableConfig = Any  # type: ignore
    AsyncSqliteSaver = Any  # type: ignore
    compile_graph = None  # type: ignore
    AetherState = Any  # type: ignore
    _LANGGRAPH_AVAILABLE = False

import json

from aether_common.contracts import (
    Event,
    EventType,
    TaskState,
    validate_transition,
)
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB
from aether_engine.langgraph.compaction import (
    compact_history,
    compaction_config_from,
    make_provider_summariser,
    resolve_summariser_provider,
)
from aether_engine.langgraph.steering import (
    drain_steers,
    enqueue_steer,
    has_pending_steers,
    steer_message_dicts,
)
from aether_engine.memory import session_store
from aether_engine.memory.facade import backend_or_none
from aether_engine.providers.base import (
    BaseProvider,
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)
from aether_engine.tools.registry import ToolRegistry

logger = logging.getLogger("aether_engine.langgraph.executor")

def _is_retriable_error(exc: Exception) -> bool:
    """Check if an exception is retriable (should trigger fallback)."""
    if isinstance(exc, ProviderError):
        return exc.retriable
    if isinstance(exc, (RateLimitError, TimeoutError, NetworkError)):
        return True
    # Check for common retriable error strings
    err_str = str(exc).lower()
    retriable_indicators = [
        "503", "502", "504", "500",
        "serviceunavailable", "service unavailable",
        "rate limit", "rate_limit", "429",
        "timeout", "timed out",
        "connection", "connecterror", "remotedisconnected",
        "network", "unreachable",
    ]
    return any(indicator in err_str for indicator in retriable_indicators)

async def run_langgraph_task(
    prompt: str,
    provider: BaseProvider,
    tools: ToolRegistry | None = None,
    task_id: str | None = None,
    request_id: str | None = None,
    max_turns: int = 25,
    approval_handler: Any | None = None,
    workspace_roots: list[str] | None = None,
    disabled_nodes: set | None = None,
    session_id: str | None = None,
    agent_models: dict[str, str] | None = None,
    agent_tools: dict[str, list[str]] | None = None,
    **kwargs,
) -> AsyncGenerator[Event, None]:
    """Execute a task prompt using the LangGraph multi-agent workflow."""
    task_id = task_id or str(uuid.uuid4())
    state_enum = TaskState.PENDING

    # Transition PENDING -> RUNNING
    validate_transition(state_enum, TaskState.RUNNING)
    state_enum = TaskState.RUNNING

    yield Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={"task_id": task_id, "prompt": prompt, "state": state_enum.value},
    )

    # Persistent checkpointer: disk-backed SQLite so paused tasks survive Engine restarts (ADR 0011 §4).
    db_path = DEFAULT_CHECKPOINT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        
        graph = compile_graph(checkpointer=saver)
        
        # Use session_id as the thread_id so chat memory persists across turns (ADR 0011)
        thread_id = session_id or request_id or str(uuid.uuid4())
        config = {
            "recursion_limit": GRAPH_RECURSION_LIMIT,
            "configurable": {
                "thread_id": thread_id,
                "tool_registry": tools,
                "approval_handler": approval_handler,
                "request_id": request_id,
                "workspace_roots": workspace_roots or [],
                "task_class": "standard",
                "provider": provider,
                "disabled_nodes": disabled_nodes or set(),
                "session_id": session_id,
                "agent_models": agent_models or {},
                "agent_tools": agent_tools or {},
                "secret_store": kwargs.get("secret_store"),
                "user_config": kwargs.get("user_config"),
                "forced_agent": kwargs.get("forced_agent"),
            }
        }
        
        initial_state = {
            "original_prompt": prompt,
            "messages": [{"role": "user", "content": prompt}],
            "plan": "No plan currently.",
            "current_phase": "initializing",
            "artifact_paths": {},
            "tool_results_summary": {},
            "pending_tool_call": None,
            "active_specialist": None,
            "blackboard": {},
            "structured_outputs": {},
            "write_tool_used": False,
            "plan_mode": bool(kwargs.get("plan_mode")),
            "delegation_log": [],
            "task_id": task_id,
            "session_id": session_id,
            "context_briefing": None,
            "active_skills": kwargs.get("active_skills", []),
        }

        # ── Conversation compaction (Epic 1, E1-06) ──────────────────
        # Fold this thread's older turns into context_summary before
        # the run starts, so every model in the run sees the summary +
        # the recent verbatim tail (via compaction.model_facing_messages)
        # instead of an unbounded history. The checkpoint's messages are
        # NOT rewritten — the full history stays stored and visible;
        # only the two summary fields are written, and only when a new
        # fold actually happened. Compaction is best-effort: any failure
        # logs and the run proceeds with the uncompacted history.
        try:
            prior_snapshot = await graph.aget_state(config)
            prior_values = prior_snapshot.values or {}
        except Exception:  # noqa: BLE001 — a fresh or unreadable thread has no history to compact; real checkpointer failures surface in the run itself.
            prior_values = {}
        if not prior_values.get("messages") and session_id:
            # Fresh thread (new session, lost checkpoint DB, or the
            # thread was invalidated by an edit-and-resend truncation):
            # seed the run from the session store's retained history so
            # the conversation continues from what the user can see —
            # never from a deleted branch, never from nothing when the
            # store still holds the earlier turns.
            try:
                seeded_history = await asyncio.to_thread(
                    session_store.get_chat_history, session_id, prompt)
            except Exception:  # noqa: BLE001 — seeding is best-effort; the run proceeds with the prompt alone.
                seeded_history = []
            if seeded_history:
                initial_state["messages"] = [
                    *seeded_history,
                    {"role": "user", "content": prompt},
                ]
        if prior_values.get("messages"):
            compacted = None
            try:
                summariser_provider = resolve_summariser_provider(
                    kwargs.get("user_config"), kwargs.get("secret_store"), provider)
                compacted = await compact_history(
                    prior_values["messages"],
                    prior_values.get("context_summary"),
                    int(prior_values.get("context_summary_upto") or 0),
                    make_provider_summariser(summariser_provider),
                    compaction_config_from(kwargs.get("user_config")),
                )
            except Exception as comp_err:  # noqa: BLE001 — best-effort by design (see block comment above).
                logger.warning(
                    f"Compaction failed for thread {thread_id}; "
                    f"continuing with the uncompacted history: {comp_err}")
            if compacted is not None:
                new_summary, new_upto = compacted
                initial_state["context_summary"] = new_summary
                initial_state["context_summary_upto"] = new_upto
                try:
                    from aether_engine.audit import AuditLogger

                    AuditLogger().log_event("CONTEXT_COMPACTED", {
                        "task_id": task_id,
                        "session_id": session_id,
                        "summary_upto": new_upto,
                    })
                except Exception:
                    # Audit is best-effort and must not fail the run
                    # (same doctrine as the registry's TOOL_EXECUTED).
                    logger.debug("CONTEXT_COMPACTED audit write failed", exc_info=True)

        start_time = time.time()
        elapsed_ms = 0.0
        full_response_parts: list[str] = []
        interrupted = False
        # Subgraph/supervisor updates carry the full echoed message and
        # delegation history; these counters keep event emission delta-only.
        messages_seen = 0
        delegation_seen = 0
        thinking_log = []
        accumulated_thoughts = []

        def record_thought(ev: Event):
            p = ev.payload
            if ev.type == EventType.TOOL_ACTIVITY:
                st = p.get("status")
                t_name = p.get("tool_name")
                if st != "pending":
                    for t in reversed(accumulated_thoughts):
                        if t.get("type") == "TOOL_ACTIVITY" and t.get("payload", {}).get("tool_name") == t_name and t.get("payload", {}).get("status") == "pending":
                            prev_args = t.get("payload", {}).get("tool_args", {})
                            new_args = p.get("tool_args", {})
                            merged_args = new_args if (new_args and len(new_args) > 0) else prev_args
                            t["payload"] = {**p, "tool_args": merged_args}
                            return
                accumulated_thoughts.append({"type": "TOOL_ACTIVITY", "payload": p, "node": p.get("node")})
            elif ev.type == EventType.NODE_ACTIVITY:
                accumulated_thoughts.append({"type": "NODE_ACTIVITY", "payload": p, "node": p.get("from_node")})
            elif ev.type == EventType.TASK_PROGRESS:
                n = p.get("node")
                d = p.get("text_delta")
                if n and n not in ("assistant", "agent") and d:
                    accumulated_thoughts.append({"node": n, "text": d})

        def emit_event(ev: Event) -> Event:
            record_thought(ev)
            return ev

        event_queue = asyncio.Queue()
        event_bus.subscribe(task_id, event_queue)

        # Steering (Epic 1, E1-07): steers for this session are drained
        # at step boundaries. The current astream segment is closed and
        # a new segment continues the SAME thread with the steers
        # appended as user messages — the checkpointer holds all prior
        # state, so nothing already emitted is replayed (the consumer's
        # delta counters keep event translation delta-only across the
        # restart).
        steering_key = session_id or thread_id

        async def _graph_runner():
            segment_input: Any = initial_state
            try:
                while True:
                    interrupted_segment = False
                    steered = False
                    stream = graph.astream(
                        segment_input, config=config, stream_mode="updates")
                    try:
                        async for evt in stream:
                            await event_queue.put(("graph", evt))
                            if isinstance(evt, dict) and "__interrupt__" in evt:
                                # Approval pause: steers stay queued;
                                # the resume path applies them.
                                interrupted_segment = True
                                break
                            if has_pending_steers(steering_key):
                                steers = drain_steers(steering_key)
                                for steer in steers:
                                    await event_queue.put(
                                        ("steering_applied", steer))
                                segment_input = {
                                    "messages": steer_message_dicts(steers)}
                                steered = True
                                break
                    finally:
                        aclose = getattr(stream, "aclose", None)
                        if aclose is not None:
                            await aclose()
                    if interrupted_segment:
                        break
                    if steered:
                        continue
                    # The stream ended: a steer that landed during the
                    # final step still belongs to this run — continue
                    # with it instead of completing without it.
                    if has_pending_steers(steering_key):
                        steers = drain_steers(steering_key)
                        for steer in steers:
                            await event_queue.put(("steering_applied", steer))
                        segment_input = {
                            "messages": steer_message_dicts(steers)}
                        continue
                    break
                await event_queue.put(("graph_done", None))
            except Exception as e:  # noqa: BLE001 — graph-run boundary: any astream failure is forwarded to the consumer, which re-raises and logs it
                await event_queue.put(("graph_error", e))

        runner_task = asyncio.create_task(_graph_runner())

        try:
            while True:
                item = await event_queue.get()
                if isinstance(item, Event):
                    if item.type == EventType.TOOL_ACTIVITY:
                        payload = item.payload
                        status = payload.get("status")
                        if status == "completed":
                            thinking_log.append(f"[TOOL] {payload.get('tool_name')}({json.dumps(payload.get('tool_args', {}))}) -> completed")
                        elif status == "failed":
                            thinking_log.append(f"[TOOL] {payload.get('tool_name')}({json.dumps(payload.get('tool_args', {}))}) -> failed")
                    yield emit_event(item)
                    continue
                    
                source, data = item
                if source == "steering_applied":
                    yield Event(
                        type=EventType.STEERING_APPLIED,
                        request_id=request_id,
                        payload={
                            "task_id": task_id,
                            "session_id": session_id,
                            "message_id": data.message_id,
                            "content": data.text,
                        },
                    )
                    continue
                if source == "graph_done":
                    break
                if source == "graph_error":
                    raise data
                    
                event = data
                for node_name, state_update in event.items():
                    if node_name == "__interrupt__":
                        intr_val = state_update[0].value
                        if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                            yield Event(
                                type=EventType.TOOL_APPROVAL_REQUEST,
                                request_id=request_id,
                                payload={
                                    "tool_name": intr_val.get("tool_name"),
                                    "args": intr_val.get("args"),
                                    "task_id": task_id,
                                    "approval_key": thread_id,
                                    "task_class": intr_val.get("task_class"),
"preview": intr_val.get("preview"),
                                    "workspace_roots": workspace_roots or [],
                                },
                            )
                            interrupted = True
                            break
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "supervisor" and isinstance(state_update.get("messages"), list):
                        messages_seen = max(messages_seen, len(state_update["messages"]))
                    if node_name == "supervisor" and len(state_update.get("delegation_log") or []) > delegation_seen:
                        delegation_seen = len(state_update["delegation_log"])
                        log_entry = state_update["delegation_log"][-1]
                        decision = log_entry.get("decision", {})
                        next_step = decision.get("next", "UNKNOWN")
                        reason = decision.get("reason", "")
                        briefing = decision.get("briefing", "")
                        updated_plan = state_update.get("plan", "")
                        
                        if decision.get("denied"):
                            text_delta = f"\n\n[Supervisor] -> Delegation to {next_step.upper()} BLOCKED by spawn policy\nReason: {reason}\n\n"
                        else:
                            text_delta = f"\n\n[Supervisor] -> Delegating to {next_step.upper()}\nReason: {reason}"
                            if briefing:
                                text_delta += f"\nBriefing: {briefing}"
                            text_delta += "\n\n"
                        
                        thinking_log.append(text_delta.strip())
                        
                        yield emit_event(Event(
                            type=EventType.NODE_ACTIVITY,
                            request_id=request_id,
                            payload={
                                "task_id": task_id,
                                "from_node": "supervisor",
                                "to_node": next_step.lower(),
                                "action": "delegation_denied" if decision.get("denied") else "delegation",
                                "reason": reason,
                                "briefing": briefing,
                                "input_payload": {
                                    "user_prompt": prompt,
                                },
                                "output_payload": {
                                    "decision": decision,
                                    "briefing": briefing if briefing else None,
                                    "assigned_specialist": next_step.lower(),
                                    "updated_plan": updated_plan,
                                },
                                "status": "completed",
                                "timestamp": int(time.time()),
                            },
                        ))
                        if decision.get("denied"):
                            # E1-11: no specialist runs for a denied
                            # delegation, so the explanation IS the
                            # task's response — emit it as one.
                            denial_text = f"Delegation to {next_step} was blocked by the spawn policy: {reason}"
                            full_response_parts.append(denial_text)
                            yield emit_event(Event(
                                type=EventType.TASK_PROGRESS,
                                request_id=request_id,
                                payload={
                                    "task_id": task_id,
                                    "text_delta": denial_text,
                                    "full_text": denial_text,
                                    "turn": 0,
                                    "node": "assistant",
                                },
                            ))
                    elif "messages" in state_update and isinstance(state_update["messages"], list):
                        new_messages = state_update["messages"][messages_seen:]
                        messages_seen = len(state_update["messages"])
                        for msg in new_messages:
                            if msg.get("role") == "assistant":
                                if msg.get("content"):
                                    full_response_parts.append(msg["content"])
                                    yield emit_event(Event(
                                        type=EventType.TASK_PROGRESS,
                                        request_id=request_id,
                                        payload={
                                            "task_id": task_id,
                                            "text_delta": msg["content"],
                                            "full_text": msg["content"],
                                            "turn": 0,
                                            "node": "assistant" if not msg.get("tool_calls") else node_name,
                                        },
                                    ))
                                if msg.get("tool_calls"):
                                    for tc in msg.get("tool_calls", []):
                                        tool_name = tc.get("function", {}).get("name", "unknown")
                                        args_str = tc.get("function", {}).get("arguments", "{}")
                                        try:
                                            tool_args = json.loads(args_str) if args_str else {}
                                        except (TypeError, ValueError):
                                            tool_args = {}
                                            
                                        thinking_log.append(f"[TOOL] {tool_name}({args_str}) -> pending")
                                        yield emit_event(Event(
                                            type=EventType.TOOL_ACTIVITY,
                                            request_id=request_id,
                                            payload={
                                                "task_id": task_id,
                                                "tool_name": tool_name,
                                                "tool_args": tool_args,
                                                "status": "pending",
                                                "node": node_name,
                                                "from_node": node_name,
                                                "to_node": "execute_tool",
                                                "timestamp": int(time.time()),
                                            },
                                        ))
                                elif msg.get("content"):
                                    yield emit_event(Event(
                                        type=EventType.NODE_ACTIVITY,
                                        request_id=request_id,
                                        payload={
                                            "task_id": task_id,
                                            "from_node": node_name,
                                            "to_node": "supervisor",
                                            "action": "step_completed",
                                            "reason": f"{node_name.capitalize()} completed reasoning and formulated output.",
                                            "input_payload": {
                                                "phase": state_update.get("current_phase", "active"),
                                            },
                                            "output_payload": {
                                                "response": msg.get("content", ""),
                                                "plan": state_update.get("plan", ""),
                                            },
                                            "status": "completed",
                                            "timestamp": int(time.time()),
                                        },
                                    ))
                            elif msg.get("role") == "tool":
                                # Handle specialist tool calls by emitting TOOL_ACTIVITY and NODE_ACTIVITY
                                tool_name = msg.get("name", "unknown")
                                tool_args = msg.get("args", {})
                                tool_result = msg.get("content", "")
                                
                                # Query state to find active specialist destination
                                current_state_obj = await graph.aget_state(config)
                                dest_node = current_state_obj.values.get("active_specialist") or "planner"
                                
                                is_failed = isinstance(tool_result, str) and tool_result.startswith(("Error", "Validation failed"))
                                status_str = "failed" if is_failed else "completed"
                                
                                thinking_log.append(f"[TOOL] {tool_name}({json.dumps(tool_args)}) -> {status_str}")
                                
                                yield emit_event(Event(
                                    type=EventType.TOOL_ACTIVITY,
                                    request_id=request_id,
                                    payload={
                                        "task_id": task_id,
                                        "tool_name": tool_name,
                                        "tool_args": tool_args,
                                        "status": status_str,
                                        "result": tool_result,
                                        "node": dest_node,
                                        "from_node": dest_node,
                                        "to_node": "execute_tool",
                                        "timestamp": int(time.time()),
                                    },
                                ))
                    
                if interrupted:
                    break

            if interrupted:
                # Phase 10.1 A2: an interrupted task is paused for approval,
                # not failed and not complete. Emit a distinct interrupted
                # outcome (state PENDING_APPROVAL) so the approval-pause ->
                # kill-engine -> restart -> resume flow can pick the thread
                # back up via its approval_key. Never TASK_FAILED/COMPLETED.
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                yield Event(
                    type=EventType.TASK_INTERRUPTED,
                    request_id=request_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.PENDING_APPROVAL.value,
                        "latency_ms": elapsed_ms,
                        "approval_key": thread_id,
                        "session_id": session_id,
                    },
                )
            else:
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                full_response = "\n".join(full_response_parts).strip()
                
                # Save the episode memory asynchronously if finished.
                # Epic 4, E4-01: through the memory facade — the
                # configured backend (native by default) decides where
                # episodes and facts live; an unavailable backend is
                # logged by backend_or_none and skips memory for this
                # completion instead of failing the task.
                if full_response:
                    memory_backend = backend_or_none(kwargs.get("user_config"), context="task completion memory")
                    if memory_backend is not None:
                        try:
                            thinking_log_str = "\n".join(thinking_log)
                            await memory_backend.record_episode(
                                task_id=task_id,
                                user_prompt=prompt,
                                task_summary=full_response[:200],  # using response snippet as summary
                                tags=["task", "completed"],
                                outcome=full_response,
                                thinking_log=thinking_log_str,
                            )
                        except Exception as db_err:  # noqa: BLE001 — best-effort episode store: a memory/DB failure is logged and must not fail the completed task
                            logger.error(f"Failed to store episode in DB for {task_id}: {db_err}")
                        # Phase 10.1 A3: extract knowledge-graph facts on the
                        # normal completion path too, with the same inputs as the
                        # resumed path (provider, task prompt, full response,
                        # task id). Previously this ran only on resume, so a task
                        # completing normally left no facts in the graph.
                        try:
                            await memory_backend.learn(
                                provider=provider,
                                user_prompt=prompt,
                                assistant_response=full_response,
                                task_id=task_id,
                            )
                        except Exception as mem_e:  # noqa: BLE001 — best-effort fact extraction (provider + DB): failure is logged and must not fail the completed task
                            logger.warning(f"Memory storage failed: {mem_e}")

                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=request_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.SUCCEEDED.value,
                        "latency_ms": elapsed_ms,
                        "response": full_response or "(task completed)",
                        "thoughts": accumulated_thoughts,
                        "session_id": session_id,
                    },
                )
        except Exception as e:
            logger.exception("LangGraph task failed")
            elapsed_ms = round((time.time() - start_time) * 1000, 2) if 'start_time' in locals() else 0.0
            logger.exception(f"Graph execution failed for task {task_id}")
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={"task_id": task_id, "error": str(e)},
            )
        finally:
            event_bus.unsubscribe(task_id, event_queue)
            runner_task.cancel()

async def resume_langgraph_task(
    thread_id: str,
    approved: bool,
    provider: BaseProvider,
    tools: ToolRegistry | None = None,
    workspace_roots: list[str] | None = None,
    disabled_nodes: set | None = None,
    agent_models: dict[str, str] | None = None,
    agent_tools: dict[str, list[str]] | None = None,
    **kwargs,
) -> AsyncGenerator[Event, None]:
    """Resume an interrupted LangGraph task and yield remaining events.

    Carries the same run configuration as ``run_langgraph_task``
    (per-agent models/tools, user config, secret store, forced agent):
    the resumed specialists and the supervisor's routing model resolve
    their providers from it exactly as they did before the pause.
    """
    db_path = DEFAULT_CHECKPOINT_DB
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        
        config = {
            "recursion_limit": GRAPH_RECURSION_LIMIT,
            "configurable": {
                "thread_id": thread_id,
                "tool_registry": tools,
                "approval_handler": kwargs.get("approval_handler"),
                "request_id": thread_id,
                "workspace_roots": workspace_roots or [],
                "task_class": "standard",
                "provider": provider,
                "disabled_nodes": disabled_nodes or set(),
                "session_id": kwargs.get("session_id"),
                "agent_models": agent_models or {},
                "agent_tools": agent_tools or {},
                "secret_store": kwargs.get("secret_store"),
                "user_config": kwargs.get("user_config"),
                "forced_agent": kwargs.get("forced_agent"),
            }
        }
        
        start_time = time.time()
        elapsed_ms = 0.0
        full_response_parts: list[str] = []
        interrupted = False
        messages_seen = 0
        delegation_seen = 0
        task_id = "unknown"
        
        try:
            state_obj = await graph.aget_state(config)
            task_id = state_obj.values.get("task_id", thread_id)
            # History from before the pause is already emitted; only
            # post-resume additions count as new output.
            messages_seen = len(state_obj.values.get("messages") or [])
            delegation_seen = len(state_obj.values.get("delegation_log") or [])

            # Steering (Epic 1, E1-07): steers queued while the task
            # waited for approval are drained now, but they cannot ride
            # the resume Command itself — the paused specialist resumes
            # from its own subgraph checkpoint and never re-reads the
            # parent messages channel (verified empirically). They are
            # applied as a fresh segment on the same thread once the
            # resume segment settles (see below); if the task pauses
            # again first, they go back on the queue untouched.
            steering_key = (
                kwargs.get("session_id")
                or state_obj.values.get("session_id")
                or thread_id
            )
            pending_steers = drain_steers(steering_key)

            async def _consume_segment(segment_input):
                nonlocal interrupted, messages_seen, delegation_seen
                async for event in graph.astream(segment_input, config=config, stream_mode="updates"):
                    for node_name, state_update in event.items():
                        if node_name == "__interrupt__":
                            intr_val = state_update[0].value
                            if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                                yield Event(
                                    type=EventType.TOOL_APPROVAL_REQUEST,
                                    request_id=thread_id,
                                    payload={
                                        "tool_name": intr_val.get("tool_name"),
                                        "args": intr_val.get("args"),
                                        "task_id": task_id,
                                        "approval_key": thread_id,
                                        "task_class": intr_val.get("task_class"),
"preview": intr_val.get("preview"),
                                        "workspace_roots": workspace_roots or [],
                                    },
                                )
                                interrupted = True
                                break
                        if not isinstance(state_update, dict):
                            continue
                        if node_name == "supervisor" and isinstance(state_update.get("messages"), list):
                            messages_seen = max(messages_seen, len(state_update["messages"]))
                        if node_name == "supervisor" and len(state_update.get("delegation_log") or []) > delegation_seen:
                            delegation_seen = len(state_update["delegation_log"])
                            log_entry = state_update["delegation_log"][-1]
                            decision = log_entry.get("decision", {})
                            next_step = decision.get("next", "UNKNOWN")
                            reason = decision.get("reason", "")
                            yield Event(
                                type=EventType.TASK_PROGRESS,
                                request_id=thread_id,
                                payload={
                                    "task_id": task_id,
                                    "text_delta": f"\\n\\n[Supervisor] -> Delegation to {next_step.upper()} BLOCKED by spawn policy\\nReason: {reason}\\n\\n" if decision.get("denied") else f"\\n\\n[Supervisor] -> Delegating to {next_step.upper()}\\nReason: {reason}\\n\\n",
                                    "full_text": "",
                                    "turn": 0,
                                    "node": node_name,
                                },
                            )
                            if decision.get("denied"):
                                # E1-11: same surfacing as the run path —
                                # the explanation is the resumed task's
                                # response when no specialist runs.
                                denial_text = f"Delegation to {next_step} was blocked by the spawn policy: {reason}"
                                full_response_parts.append(denial_text)
                                yield Event(
                                    type=EventType.TASK_PROGRESS,
                                    request_id=thread_id,
                                    payload={
                                        "task_id": task_id,
                                        "text_delta": denial_text,
                                        "full_text": denial_text,
                                        "turn": 0,
                                        "node": "assistant",
                                    },
                                )
                        elif "messages" in state_update and isinstance(state_update["messages"], list):
                            new_messages = state_update["messages"][messages_seen:]
                            messages_seen = len(state_update["messages"])
                            for msg in new_messages:
                                if msg.get("role") == "assistant" and msg.get("content"):
                                    full_response_parts.append(msg["content"])
                                    yield Event(
                                        type=EventType.TASK_PROGRESS,
                                        request_id=thread_id,
                                        payload={
                                            "task_id": task_id,
                                            "text_delta": msg["content"],
                                            "full_text": msg["content"],
                                            "turn": 0,
                                            "node": node_name,
                                        },
                                    )
                                elif msg.get("role") == "tool":
                                    yield Event(
                                        type=EventType.TASK_PROGRESS,
                                        request_id=thread_id,
                                        payload={
                                            "task_id": task_id,
                                            "text_delta": f"\\n\\n[Tool Executed: {msg.get('name')}]\\n",
                                            "full_text": f"\\n\\n[Tool Executed: {msg.get('name')}]\\n",
                                            "turn": 0,
                                            "node": node_name,
                                        },
                                    )
                    if interrupted:
                        break



            interrupted = False
            async for _ev in _consume_segment(Command(resume=approved)):
                yield _ev

            if pending_steers and not interrupted:
                # The resumed segment settled: apply the steers as a
                # fresh segment on the same thread, where the next
                # provider call re-reads the full state and sees them.
                for steer in pending_steers:
                    yield Event(
                        type=EventType.STEERING_APPLIED,
                        request_id=thread_id,
                        payload={
                            "task_id": task_id,
                            "session_id": state_obj.values.get("session_id"),
                            "message_id": steer.message_id,
                            "content": steer.text,
                        },
                    )
                async for _ev in _consume_segment(
                        {"messages": steer_message_dicts(pending_steers)}):
                    yield _ev
            elif pending_steers:
                # Paused again before the steers could be applied:
                # return them to the queue for the next resume.
                for steer in pending_steers:
                    enqueue_steer(steering_key, steer.text, steer.message_id)
            if interrupted:
                # Phase 10.1 A2: same as the run path -- a re-interrupt on
                # resume is still a pause for approval, not a failure.
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                yield Event(
                    type=EventType.TASK_INTERRUPTED,
                    request_id=thread_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.PENDING_APPROVAL.value,
                        "latency_ms": elapsed_ms,
                        "approval_key": thread_id,
                        "session_id": state_obj.values.get("session_id"),
                    },
                )
            else:
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                full_response = "\\n".join(full_response_parts).strip()
                
                orig_prompt = state_obj.values.get("original_prompt", "")
                # Epic 4, E4-01: same facade path as fresh completions.
                memory_backend = backend_or_none(
                    config["configurable"].get("user_config"), context="resumed task memory")
                if memory_backend is not None:
                    try:
                        await memory_backend.record_episode(task_id, orig_prompt, full_response, ["task", "completed", "resumed"], TaskState.SUCCEEDED.value)
                        await memory_backend.learn(
                            provider=provider,
                            user_prompt=orig_prompt,
                            assistant_response=full_response,
                            task_id=task_id
                        )
                    except Exception as mem_e:  # noqa: BLE001 — best-effort memory store on the resume path: failure is logged and must not fail the completed task
                        logger.warning(f"Memory storage failed: {mem_e}")

                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=thread_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.SUCCEEDED.value,
                        "latency_ms": elapsed_ms,
                        "response": full_response or "(task completed)",
                        "session_id": state_obj.values.get("session_id"),
                    },
                )
        except Exception as e:
            logger.exception("LangGraph task failed during resume")
            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            retriable = _is_retriable_error(e)
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=thread_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.FAILED.value,
                    "error": str(e),
                    "retriable": retriable,
                    "latency_ms": elapsed_ms,
                },
            )

async def get_pending_approvals() -> list[dict[str, Any]]:
    """Query the checkpointer for threads blocked on an approval request."""
    db_path = DEFAULT_CHECKPOINT_DB
    pending = []
    
    # We gracefully skip if the DB isn't initialized yet
    if not db_path.exists():
        return pending
        
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        
        # We need aiosqlite to query thread_ids, or we can use the saver if it exposes it
        # AsyncSqliteSaver doesn't expose a list_threads natively in 1.0.1 in a simple way.
        # But we can query the sqlite DB directly.
        import aiosqlite
        async with (
            aiosqlite.connect(db_path) as db,
            db.execute("SELECT DISTINCT thread_id FROM checkpoints") as cursor,
        ):
            threads = [row[0] async for row in cursor]
                
        for t_id in threads:
            try:
                config = {"configurable": {"thread_id": t_id}}
                state = await graph.aget_state(config)
                for task in state.tasks:
                    if task.interrupts:
                        intr_val = task.interrupts[0].value
                        if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                            pending.append({
                                "thread_id": t_id,
                                "task_id": state.values.get("task_id", t_id),
                                "tool_name": intr_val.get("tool_name"),
                                "args": intr_val.get("args"),
                                "task_class": intr_val.get("task_class", "standard"),
                                "preview": intr_val.get("preview")
                            })
                            break
            except Exception as e:  # noqa: BLE001 — per-thread scan isolation: one unreadable thread is logged and must not abort the approvals listing
                logger.warning(f"Error checking thread {t_id}: {e}")
                
    return pending
