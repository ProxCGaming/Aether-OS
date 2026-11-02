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
    from aether_engine.langgraph.state import (
        AetherState,
        message_dicts,
        multimodal_user_content,
    )
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    RunnableConfig = Any  # type: ignore
    AsyncSqliteSaver = Any  # type: ignore
    compile_graph = None  # type: ignore
    AetherState = Any  # type: ignore
    message_dicts = None  # type: ignore
    multimodal_user_content = None  # type: ignore
    _LANGGRAPH_AVAILABLE = False

import json

from aether_common.contracts import (
    Event,
    EventType,
    TaskState,
    validate_transition,
)
from aether_engine.artifacts.store import get_artifact_store
from aether_engine.config import UserConfig
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
    steer_state_update,
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
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.tools.registry import ToolRegistry

logger = logging.getLogger("aether_engine.langgraph.executor")

# ---------------------------------------------------------------------------
# Epic 8, E8-1.1a: rewind manifest recording. As a graph task runs, each
# landed superstep appends a point (step -> checkpoint, label,
# files-written-so-far) to the session's manifest for THIS task. The
# tracker is a plain dict threaded through a run (and re-seeded from
# the manifest on resume):
#   {"step_index": int, "last_checkpoint_id": str, "files_written": [str],
#    "pending_write_events": [str]}
# "files_written" is the cumulative UNIQUE list (the picker's trail);
# "pending_write_events" buffers every landed write since the last
# recorded point — repeats included (N01) — and becomes that point's
# write events, which is what the not-reverted warning reads.
# Recording is best-effort by doctrine — like compaction and completion
# memory, a manifest failure is logged and never fails the task.
# ---------------------------------------------------------------------------

_WRITE_TOOL_NAMES = frozenset({"write_file", "edit_file"})

_SUPERSTEP_LABELS = {
    "supervisor": "Supervisor",
    "planner": "Planner",
    "coder": "Coder",
    "researcher": "Researcher",
    "execute_tool": "Tool execution",
}


def _new_manifest_tracker(prior_checkpoint_id: str = "") -> dict[str, Any]:
    return {
        "step_index": 0,
        "last_checkpoint_id": prior_checkpoint_id,
        "files_written": [],
        "pending_write_events": [],
    }


def _note_manifest_file_write(
    tracker: dict[str, Any], msg: dict[str, Any], is_failed: bool
) -> None:
    """Track a completed file write from a tool message's args.

    The cumulative list keeps each path once; the event buffer keeps
    EVERY landed write (N01) — a repeat write to a listed path is
    invisible in the cumulative list but is exactly what the
    not-reverted warning needs to see.
    """
    if is_failed or msg.get("name") not in _WRITE_TOOL_NAMES:
        return
    args = msg.get("args") or {}
    path = args.get("path") or args.get("file_path")
    if not path:
        return
    tracker["pending_write_events"].append(path)
    if path not in tracker["files_written"]:
        tracker["files_written"].append(path)


async def _record_manifest_point(
    session_id: str,
    task_id: str,
    step_index: int,
    checkpoint_id: str,
    label: str,
    files_written: list[str],
    write_events: list[str] | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            rewind_manifest.record_point,
            session_id, task_id, step_index, checkpoint_id, label,
            list(files_written), list(write_events or []),
        )
    except Exception:  # best-effort by doctrine (see block comment above)
        logger.debug("Rewind manifest recording failed", exc_info=True)


async def _advance_manifest_to_checkpoint(
    graph,
    config,
    tracker: dict[str, Any],
    *,
    session_id: str | None,
    task_id: str,
    label: str,
) -> None:
    """Append a point if the graph's checkpoint advanced past the
    tracker's last recorded one (one point per landed superstep)."""
    if not session_id:
        return
    try:
        snapshot = await graph.aget_state(config)
        checkpoint_id = (
            (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            or ""
        )
    except Exception:  # noqa: BLE001 — best-effort by doctrine (see block comment above)
        return
    if not checkpoint_id or checkpoint_id == tracker["last_checkpoint_id"]:
        return
    tracker["last_checkpoint_id"] = checkpoint_id
    tracker["step_index"] += 1
    # The writes noted since the previous point become THIS point's
    # events (N01); the buffer then restarts for the next point.
    write_events = list(tracker["pending_write_events"])
    tracker["pending_write_events"] = []
    await _record_manifest_point(
        session_id, task_id, tracker["step_index"], checkpoint_id, label,
        tracker["files_written"], write_events,
    )


def _superstep_label(event: dict, step_index: int) -> str:
    for key in event:
        if not key.startswith("__"):
            pretty = _SUPERSTEP_LABELS.get(
                key, key.replace("_", " ").capitalize())
            return f"After {pretty}"
    return f"Step {step_index}"


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


def _messages_have_images(messages: Any) -> bool:
    """True when any message carries an image part (M17 resume check).

    Checkpointed messages use the multimodal parts shape
    (``content`` is a list of ``{"type": "image_url", ...}`` dicts)
    once a task has shown the model an image; plain-text turns are
    plain strings and never match.
    """
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False

def _run_config_snapshot(
    *,
    provider: BaseProvider,
    session_id: str | None,
    agent_models: dict[str, str] | None,
    agent_tools: dict[str, list[str]] | None,
    workspace_roots: list[str] | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """The run's effective configuration as plain JSON-safe data.

    Seeded into the graph state (``run_config``) so the checkpointer
    carries it across an approval pause: E1-F04 — a resume rebuilds
    the paused task's OWN provider, per-agent models/tools, forced
    agent, workspace roots and layered user config from this
    snapshot instead of discarding them for the current globals.
    Only names and values are recorded; live objects (provider
    instance, tool registry, secret store) are rebuilt at resume
    time and never enter the checkpoint.
    """
    user_config = kwargs.get("user_config")
    return {
        "provider_name": getattr(provider, "provider_name", None),
        "model": getattr(provider, "model", None),
        "session_id": session_id,
        "agent_models": dict(agent_models or {}),
        "agent_tools": {
            name: list(tools) for name, tools in (agent_tools or {}).items()
        },
        "forced_agent": kwargs.get("forced_agent"),
        "workspace_roots": [str(root) for root in (workspace_roots or [])],
        "user_config": (
            user_config.to_dict() if isinstance(user_config, UserConfig) else None
        ),
        "local_only": bool(kwargs.get("local_only")),
        "require_vision": bool(kwargs.get("require_vision")),
    }


async def get_persisted_run_config(thread_id: str) -> dict[str, Any] | None:
    """Read the run-config snapshot checkpointed with a thread's state.

    Returns None when the thread has no snapshot (a task paused
    before snapshots existed, or an unreadable checkpoint) — callers
    fall back to their previous behaviour for those threads.
    """
    db_path = DEFAULT_CHECKPOINT_DB
    if not db_path.exists():
        return None
    try:
        async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
            await saver.setup()
            graph = compile_graph(checkpointer=saver)
            state = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}})
            snapshot = (state.values or {}).get("run_config")
            return snapshot if isinstance(snapshot, dict) else None
    except Exception:  # best-effort read: a resume must never fail because the snapshot could not be loaded
        logger.debug(
            f"Could not read persisted run config for thread {thread_id}",
            exc_info=True)
        return None


def _turn_text(content: Any) -> str:
    """Plain text of a message content: strings pass through;
    multimodal part lists contribute their text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _turn_key(role: str, text: str) -> tuple[str, str]:
    return role, " ".join(text.split())


def _missing_store_turns(
    checkpoint_messages: Any, store_history: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Session-store turns the graph checkpoint does not hold (E1-F05).

    The session store is the visible conversation: it also holds the
    fast-chat turns, which never enter the graph checkpoint. Matching
    is an in-order subsequence walk — each store turn consumes the
    first identical checkpoint turn at or after the previous match —
    so graph turns are recognised as present (never duplicated) while
    turns that happened outside the graph are returned, in store
    order, for the caller to append ahead of the new prompt.
    """
    checkpoint_keys: list[tuple[str, str]] = []
    for msg in message_dicts(checkpoint_messages):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _turn_text(msg.get("content"))
        if text.strip():
            checkpoint_keys.append(_turn_key(role, text))
    missing: list[dict[str, str]] = []
    cursor = 0
    for turn in store_history:
        key = _turn_key(turn["role"], turn["content"])
        try:
            cursor = checkpoint_keys.index(key, cursor) + 1
        except ValueError:
            missing.append({"role": turn["role"], "content": turn["content"]})
    return missing


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

    # Epic 8 (review finding M16): the websocket forwards the user's
    # attachments here; build the user message's content ONCE in the
    # multimodal parts shape fast chat uses, so the image reaches the
    # assigned specialist's own model through graph init, history
    # seeding, handoff and checkpointing. Text-only tasks keep the
    # plain string content, byte-identical to before.
    user_content = multimodal_user_content(
        prompt, kwargs.get("attachments"), workspace_roots=workspace_roots)

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
                # N08: originating user message id (media provenance);
                # also seeded into state below, which is the channel
                # that survives the approval pause.
                "message_id": kwargs.get("message_id"),
                # Finding M17 (residual): the routing decision's
                # constraints ride the run config so providers built
                # INSIDE the run (specialist overrides, the supervisor
                # resolver, the compaction summariser) honour them —
                # a local-only vision task must not acquire a cloud
                # fallback one construction level down.
                "local_only": bool(kwargs.get("local_only")),
                "require_vision": bool(kwargs.get("require_vision")),
            }
        }

        # Epic 7, E7-04: skill auto-match — previously match_skills
        # had no caller, so skills activated only via slash commands
        # or per-agent attachment. Keyword matches against the prompt
        # join the explicitly activated set (deduped, manager-capped).
        effective_skills = list(kwargs.get("active_skills", []))
        try:
            from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

            for match in GLOBAL_SKILL_MANAGER.match_skills(prompt, "supervisor"):
                if match.name not in effective_skills:
                    effective_skills.append(match.name)
        except Exception:  # skill matching must never break a task start
            logger.debug("Skill auto-match failed", exc_info=True)

        initial_state = {
            "original_prompt": prompt,
            "messages": [{"role": "user", "content": user_content}],
            "plan": "No plan currently.",
            "current_phase": "initializing",
            "artifact_paths": {},
            "tool_results_summary": {},
            "pending_tool_call": None,
            "queued_tool_calls": [],
            "active_specialist": None,
            "blackboard": {},
            "structured_outputs": {},
            "write_tool_used": False,
            "plan_mode": bool(kwargs.get("plan_mode")),
            "delegation_log": [],
            "task_id": task_id,
            "session_id": session_id,
            "message_id": kwargs.get("message_id"),
            "context_briefing": None,
            "active_skills": effective_skills,
            # E1-F04: this run's effective configuration, checkpointed
            # with the state so an approval resume rebuilds the SAME
            # configuration (see _run_config_snapshot).
            "run_config": _run_config_snapshot(
                provider=provider,
                session_id=session_id,
                agent_models=agent_models,
                agent_tools=agent_tools,
                workspace_roots=workspace_roots,
                kwargs=kwargs,
            ),
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
        prior_checkpoint_id = ""
        try:
            prior_snapshot = await graph.aget_state(config)
            prior_values = prior_snapshot.values or {}
            prior_checkpoint_id = (
                (prior_snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
                or ""
            )
        except Exception:  # noqa: BLE001 — a fresh or unreadable thread has no history to compact; real checkpointer failures surface in the run itself.
            prior_values = {}
        if session_id:
            # The session store holds the whole visible conversation,
            # fast-chat turns included; the graph checkpoint holds
            # only graph turns. The store is consulted for EVERY run
            # (E1-F05), not just fresh threads — best-effort either
            # way: a store failure leaves the run with the prompt
            # alone, exactly as before.
            try:
                store_history = await asyncio.to_thread(
                    session_store.get_chat_history, session_id, prompt)
            except Exception:  # noqa: BLE001 — history is best-effort; the run proceeds with the prompt alone.
                store_history = []
            if not prior_values.get("messages"):
                # Fresh thread (new session, lost checkpoint DB, or
                # the thread was invalidated by an edit-and-resend
                # truncation): seed the run from the session store's
                # retained history so the conversation continues from
                # what the user can see — never from a deleted branch,
                # never from nothing when the store still holds the
                # earlier turns.
                if store_history:
                    initial_state["messages"] = [
                        *store_history,
                        {"role": "user", "content": user_content},
                    ]
            else:
                # Existing checkpoint: turns taken in fast chat since
                # the last graph run live only in the store. Append
                # the ones the checkpoint lacks, in store order, ahead
                # of the new prompt — or the work model answers
                # without what the user already said in chat.
                missing_turns = _missing_store_turns(
                    prior_values["messages"], store_history)
                if missing_turns:
                    initial_state["messages"] = [
                        *missing_turns,
                        {"role": "user", "content": user_content},
                    ]
        if prior_values.get("messages"):
            compacted = None
            try:
                summariser_provider = resolve_summariser_provider(
                    kwargs.get("user_config"), kwargs.get("secret_store"), provider,
                    local_only=bool(kwargs.get("local_only")))
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

        # Epic 8, E8-1.1a: open this task's rewind manifest. Step 0 is
        # the "before this task began" point, anchored at the thread's
        # pre-run checkpoint ("" on a fresh thread — the first landed
        # superstep's checkpoint becomes step 1).
        manifest_tracker = _new_manifest_tracker(prior_checkpoint_id)
        if session_id:
            await _record_manifest_point(
                session_id, task_id, 0, prior_checkpoint_id,
                "Before this task began", manifest_tracker["files_written"])

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
                                # PF-3: the update also moves the
                                # state's message_id provenance to the
                                # steer message (steer_state_update).
                                segment_input = steer_state_update(steers)
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
                        segment_input = steer_state_update(steers)
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
"browser_confirmation": intr_val.get("browser_confirmation"),
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
                                # E8-1.1a: a landed write joins the
                                # manifest's files-written-so-far before
                                # this superstep's point is recorded.
                                _note_manifest_file_write(
                                    manifest_tracker, msg, is_failed)

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
                                        "edited": bool(msg.get("edited")),
                                        "node": dest_node,
                                        "from_node": dest_node,
                                        "to_node": "execute_tool",
                                        "timestamp": int(time.time()),
                                    },
                                ))
                    
                # Epic 8, E8-1.1a: the superstep landed — record its
                # rewind point (deduped on checkpoint id inside).
                await _advance_manifest_to_checkpoint(
                    graph, config, manifest_tracker,
                    session_id=session_id, task_id=task_id,
                    label=_superstep_label(
                        event, manifest_tracker["step_index"] + 1))

                if interrupted:
                    break

            if not interrupted:
                # Final point at the run's end checkpoint (a no-op if
                # the last superstep already recorded it).
                await _advance_manifest_to_checkpoint(
                    graph, config, manifest_tracker,
                    session_id=session_id, task_id=task_id,
                    label="Task complete")

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
                        # Epic 4, E4-10 (addendum omp #29): the learn
                        # loop — explicit lessons from this task go
                        # into learned.md for the next session's
                        # briefing. Same best-effort contract as fact
                        # extraction, its own guard so a lesson
                        # failure can never mask or break the rest.
                        try:
                            await memory_backend.learn_lessons(
                                provider=provider,
                                user_prompt=prompt,
                                assistant_response=full_response,
                                task_id=task_id,
                            )
                        except Exception as lesson_e:  # noqa: BLE001 — best-effort lesson extraction: failure is logged and must not fail the completed task
                            logger.warning(f"Lesson extraction failed: {lesson_e}")

                # Epic 8, E8-2.4: auto-record task final summary as a report document artifact
                final_summary_text = full_response or "(task completed)"
                try:
                    art_store = get_artifact_store()
                    summary_art = art_store.record_summary_artifact(
                        session_id=session_id or "default",
                        task_id=task_id,
                        summary=final_summary_text,
                    )
                    event_bus.publish(
                        task_id,
                        Event(
                            type=EventType.ARTIFACT_RECORDED,
                            request_id=task_id,
                            payload={
                                "artifact_id": summary_art["id"],
                                "session_id": summary_art["session_id"],
                                "task_id": summary_art["task_id"],
                                "kind": summary_art["kind"],
                                "title": summary_art["title"],
                                "path": summary_art.get("file_path"),
                                "created_at": summary_art["created_at"],
                            },
                        ),
                    )
                    yield emit_event(Event(
                        type=EventType.ARTIFACT_RECORDED,
                        request_id=request_id,
                        payload={
                            "artifact_id": summary_art["id"],
                            "session_id": summary_art["session_id"],
                            "task_id": summary_art["task_id"],
                            "kind": summary_art["kind"],
                            "title": summary_art["title"],
                            "path": summary_art.get("file_path"),
                            "created_at": summary_art["created_at"],
                        },
                    ))
                except Exception as art_err:  # noqa: BLE001 — summary artifact store is best-effort and must not fail the completed task
                    logger.warning(f"Failed to record summary artifact: {art_err}")

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
                payload={
                    "task_id": task_id,
                    "state": TaskState.FAILED.value,
                    "error": str(e),
                    "retriable": _is_retriable_error(e),
                    "latency_ms": elapsed_ms,
                },
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
    edited_args: dict[str, Any] | None = None,
    browser_envelope: dict[str, Any] | None = None,
    **kwargs,
) -> AsyncGenerator[Event, None]:
    """Resume an interrupted LangGraph task and yield remaining events.

    Carries the same run configuration as ``run_langgraph_task``
    (per-agent models/tools, user config, secret store, forced agent):
    the resumed specialists and the supervisor's routing model resolve
    their providers from it exactly as they did before the pause.

    Finding M17 (residual): the routing constraints ride along too.
    Callers may pass ``local_only`` / ``require_vision`` explicitly;
    when they don't, they are re-derived here — ``local_only`` from
    the same auxiliary vision override the original routing decision
    read, ``require_vision`` from the checkpointed history once it is
    loaded below — so the constraint cannot lapse across a pause.
    """
    _local_only = kwargs.get("local_only")
    if _local_only is None:
        from aether_engine.routing.policy import local_only_from_vision_override

        _local_only = local_only_from_vision_override(
            getattr(kwargs.get("user_config"), "auxiliary_models", None))
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
                "local_only": bool(_local_only),
                "require_vision": bool(kwargs.get("require_vision")),
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
            # E1-F04: fill any run configuration the caller did not
            # supply from the snapshot the run checkpointed with its
            # state — the resume continues the PAUSED task's execution
            # settings, not whatever the caller happens to hold.
            # Caller-supplied values always win (the websocket route
            # passes its own snapshot read); this is the net for
            # direct executor callers.
            _snapshot = state_obj.values.get("run_config")
            if isinstance(_snapshot, dict):
                _cfg = config["configurable"]
                if not _cfg.get("session_id"):
                    _cfg["session_id"] = _snapshot.get("session_id")
                if not _cfg.get("forced_agent"):
                    _cfg["forced_agent"] = _snapshot.get("forced_agent")
                if not _cfg.get("agent_models") and _snapshot.get("agent_models"):
                    _cfg["agent_models"] = dict(_snapshot["agent_models"])
                if not _cfg.get("agent_tools") and _snapshot.get("agent_tools"):
                    _cfg["agent_tools"] = {
                        name: list(tools)
                        for name, tools in _snapshot["agent_tools"].items()
                    }
                if _cfg.get("user_config") is None and isinstance(
                        _snapshot.get("user_config"), dict):
                    try:
                        _cfg["user_config"] = UserConfig.from_dict(
                            _snapshot["user_config"])
                    except Exception:  # a corrupt snapshot must not block the resume
                        logger.debug(
                            "Could not restore run user_config from snapshot",
                            exc_info=True)
            # M17 residual: if the caller did not state require_vision,
            # derive it from the checkpointed history — the resumed
            # specialists are shown those same image parts, so their
            # provider constructions must keep the vision constraint.
            if not kwargs.get("require_vision"):
                config["configurable"]["require_vision"] = (
                    _messages_have_images(state_obj.values.get("messages")))
            # History from before the pause is already emitted; only
            # post-resume additions count as new output.
            messages_seen = len(state_obj.values.get("messages") or [])
            delegation_seen = len(state_obj.values.get("delegation_log") or [])

            # Epic 8, E8-1.1a: the resumed task's manifest continues —
            # seed the tracker from its last recorded point so step
            # indexes and files-written-so-far carry over the pause.
            resume_session_id = (
                state_obj.values.get("session_id") or kwargs.get("session_id"))
            manifest_tracker = _new_manifest_tracker()
            if resume_session_id:
                try:
                    existing_entries = await asyncio.to_thread(
                        rewind_manifest.get_entries, resume_session_id, task_id)
                except Exception:  # noqa: BLE001 — best-effort by doctrine (see the recording block comment)
                    existing_entries = []
                if existing_entries:
                    last_entry = existing_entries[-1]
                    manifest_tracker["step_index"] = last_entry["step_index"]
                    manifest_tracker["last_checkpoint_id"] = (
                        last_entry["checkpoint_id"])
                    manifest_tracker["files_written"] = list(
                        last_entry["files_written"])
                else:
                    # A thread with no recorded points (pre-manifest
                    # task): its first resumed point becomes step 0.
                    manifest_tracker["step_index"] = -1

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
"browser_confirmation": intr_val.get("browser_confirmation"),
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
                                    # Mirror the run path's TOOL_ACTIVITY
                                    # for the executed call so the pending
                                    # tool card completes after a resume —
                                    # including the Epic 8 E8-1.3 "edited"
                                    # marker for approval-time arg edits.
                                    tool_result = msg.get("content", "")
                                    is_failed = isinstance(tool_result, str) and tool_result.startswith(("Error", "Validation failed"))
                                    # E8-1.1a: same files-written tracking
                                    # as the run path.
                                    _note_manifest_file_write(
                                        manifest_tracker, msg, is_failed)
                                    yield Event(
                                        type=EventType.TOOL_ACTIVITY,
                                        request_id=thread_id,
                                        payload={
                                            "task_id": task_id,
                                            "tool_name": msg.get("name", "unknown"),
                                            "tool_args": msg.get("args", {}),
                                            "status": "failed" if is_failed else "completed",
                                            "result": tool_result,
                                            "edited": bool(msg.get("edited")),
                                            "node": node_name,
                                            "from_node": node_name,
                                            "to_node": "execute_tool",
                                            "timestamp": int(time.time()),
                                        },
                                    )
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
                    # Epic 8, E8-1.1a: record the resumed segment's
                    # landed superstep in the same task's manifest.
                    await _advance_manifest_to_checkpoint(
                        graph, config, manifest_tracker,
                        session_id=resume_session_id, task_id=task_id,
                        label=_superstep_label(
                            event, manifest_tracker["step_index"] + 1))
                    if interrupted:
                        break



            interrupted = False
            # Epic 8, E8-1.3: approval-time argument edits ride the
            # resume value as a dict; an unedited approval keeps the
            # legacy bare-boolean resume exactly as before.
            # Epic 8, E8-6.2: a browser-envelope grant granted at a
            # confirmation rides the same dict (the pause node scopes
            # it to the confirmed action's category + site itself).
            resume_value: Any = approved
            if approved and (isinstance(edited_args, dict)
                             or isinstance(browser_envelope, dict)):
                resume_value = {"approved": True}
                if isinstance(edited_args, dict):
                    resume_value["edited_args"] = edited_args
                if isinstance(browser_envelope, dict):
                    resume_value["browser_envelope"] = browser_envelope
            async for _ev in _consume_segment(Command(resume=resume_value)):
                yield _ev

            # Apply the drained steers as fresh segments on the same
            # thread, where the next provider call re-reads the full
            # state and sees them. E1-F06: steers keep arriving while
            # the resumed run works, so every settled segment is
            # followed by a fresh drain — a steer that landed during
            # the resume segment (or during a steer segment) is
            # applied by THIS task instead of being stranded in the
            # queue when the task completes.
            while not interrupted:
                if not pending_steers:
                    # Nothing was waiting at entry (or the previous
                    # batch just applied): pick up whatever arrived
                    # while the last segment was running.
                    pending_steers = drain_steers(steering_key)
                if not pending_steers:
                    break
                steers_to_apply = pending_steers
                pending_steers = []
                for steer in steers_to_apply:
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
                        steer_state_update(steers_to_apply)):
                    yield _ev
            if pending_steers:
                # Paused again before the steers could be applied:
                # return them to the queue for the next resume.
                for steer in pending_steers:
                    # PF-1: attachments ride the re-enqueue too, or a
                    # steer that pauses twice loses its images.
                    enqueue_steer(
                        steering_key, steer.text, steer.message_id,
                        steer.attachments)
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
                        # Epic 4, E4-10: lessons on the resume path
                        # too — a resumed task ends the same way.
                        await memory_backend.learn_lessons(
                            provider=provider,
                            user_prompt=orig_prompt,
                            assistant_response=full_response,
                            task_id=task_id
                        )
                    except Exception as mem_e:  # noqa: BLE001 — best-effort memory store on the resume path: failure is logged and must not fail the completed task
                        logger.warning(f"Memory storage failed: {mem_e}")

                # Epic 8, E8-2.4: auto-record task final summary as a report document artifact
                final_summary_text = full_response or "(task completed)"
                sess_id = state_obj.values.get("session_id") or "default"
                try:
                    art_store = get_artifact_store()
                    summary_art = art_store.record_summary_artifact(
                        session_id=sess_id,
                        task_id=task_id,
                        summary=final_summary_text,
                    )
                    event_bus.publish(
                        task_id,
                        Event(
                            type=EventType.ARTIFACT_RECORDED,
                            request_id=task_id,
                            payload={
                                "artifact_id": summary_art["id"],
                                "session_id": summary_art["session_id"],
                                "task_id": summary_art["task_id"],
                                "kind": summary_art["kind"],
                                "title": summary_art["title"],
                                "path": summary_art.get("file_path"),
                                "created_at": summary_art["created_at"],
                            },
                        ),
                    )
                    yield Event(
                        type=EventType.ARTIFACT_RECORDED,
                        request_id=thread_id,
                        payload={
                            "artifact_id": summary_art["id"],
                            "session_id": summary_art["session_id"],
                            "task_id": summary_art["task_id"],
                            "kind": summary_art["kind"],
                            "title": summary_art["title"],
                            "path": summary_art.get("file_path"),
                            "created_at": summary_art["created_at"],
                        },
                    )
                except Exception as art_err:  # noqa: BLE001 — summary artifact store is best-effort and must not fail the completed task
                    logger.warning(f"Failed to record summary artifact on resume: {art_err}")

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
                                "preview": intr_val.get("preview"),
                                "browser_confirmation": intr_val.get("browser_confirmation")
                            })
                            break
            except Exception as e:  # noqa: BLE001 — per-thread scan isolation: one unreadable thread is logged and must not abort the approvals listing
                logger.warning(f"Error checking thread {t_id}: {e}")
                
    return pending
