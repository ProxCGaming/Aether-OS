import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

try:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    from aether_engine.langgraph.graph import (
        GRAPH_RECURSION_LIMIT,
        compile_graph,
        supervisor_pattern_context,
    )
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

    from contextlib import contextmanager as _contextmanager

    @_contextmanager
    def supervisor_pattern_context(pattern=None):  # type: ignore
        yield
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
from aether_engine.event_bus import EVENT_QUEUE_MAXSIZE, event_bus
from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB
from aether_engine.langgraph.compaction import (
    compact_history,
    compaction_config_from,
    make_provider_summariser,
    resolve_summariser_provider,
)

# Wave 5: the manifest tracker, the run-config helpers and the
# stream-interpretation helpers live in their own modules
# (langgraph.manifest / langgraph.run_config / langgraph.streaming)
# and are re-exported here — this module's namespace is the
# historical import/patch surface for all three families (see
# __all__ below).
from aether_engine.langgraph.manifest import (
    _WRITE_TOOL_NAMES,
    _advance_manifest_to_checkpoint,
    _new_manifest_tracker,
    _note_manifest_file_write,
    _record_manifest_point,
)
from aether_engine.langgraph.run_config import (
    _missing_store_turns,
    _run_config_snapshot,
    _turn_key,
    _turn_text,
    get_persisted_run_config,
)
from aether_engine.langgraph.steering import (
    drain_steers,
    enqueue_steer,
    has_pending_steers,
    steer_state_update,
)
from aether_engine.langgraph.streaming import (
    _SUPERSTEP_LABELS,
    _is_retriable_error,
    _superstep_label,
    merge_segment_items,
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
from aether_engine.task_ledger import record_executor_event
from aether_engine.tools.registry import ToolRegistry

logger = logging.getLogger("aether_engine.langgraph.executor")

# The namespace is contractual: the suites import and patch names
# here (compile_graph, DEFAULT_CHECKPOINT_DB, the manifest,
# run-config and streaming helpers re-exported above),
# routes/websocket.py and langgraph/fast_chat.py import through
# here, and rewind.service reads DEFAULT_CHECKPOINT_DB off this
# module. __all__ pins the full surface so the Wave 5 splits
# cannot silently shrink it.
__all__ = [
    "DEFAULT_CHECKPOINT_DB",
    "EVENT_QUEUE_MAXSIZE",
    "GRAPH_EVENT_IDLE_TIMEOUT_SECONDS",
    "GRAPH_RECURSION_LIMIT",
    "_DELEGATION_HANDOFF_PREFIXES",
    "_LANGGRAPH_AVAILABLE",
    "_PARALLEL_GROUP_TOOL_NAME",
    "_RUNNER_SHUTDOWN_GRACE_SECONDS",
    "_SUPERSTEP_LABELS",
    "_WRITE_TOOL_NAMES",
    "AetherState",
    "Any",
    "AsyncGenerator",
    "AsyncSqliteSaver",
    "BaseProvider",
    "Command",
    "Event",
    "EventType",
    "NetworkError",
    "ProviderError",
    "RateLimitError",
    "RunnableConfig",
    "TaskState",
    "TimeoutError",
    "ToolRegistry",
    "UserConfig",
    "_advance_manifest_to_checkpoint",
    "_delegation_activity_event",
    "_is_delegation_result_message",
    "_is_handoff_message",
    "_is_retriable_error",
    "_messages_have_images",
    "_missing_store_turns",
    "_new_delegation_events",
    "_new_manifest_tracker",
    "_note_manifest_file_write",
    "_reap_runner",
    "_record_manifest_point",
    "_resume_thread_impl",
    "_run_config_snapshot",
    "_superstep_label",
    "_turn_key",
    "_turn_text",
    "asyncio",
    "backend_or_none",
    "compact_history",
    "compaction_config_from",
    "compile_graph",
    "contextlib",
    "continue_langgraph_task",
    "drain_steers",
    "enqueue_steer",
    "event_bus",
    "get_artifact_store",
    "get_pending_approvals",
    "get_persisted_run_config",
    "has_pending_steers",
    "json",
    "logger",
    "logging",
    "make_provider_summariser",
    "merge_segment_items",
    "message_dicts",
    "multimodal_user_content",
    "record_executor_event",
    "resolve_summariser_provider",
    "resume_langgraph_task",
    "rewind_manifest",
    "run_langgraph_task",
    "session_store",
    "steer_state_update",
    "supervisor_pattern_context",
    "time",
    "uuid",
    "validate_transition",
]

# N-06: idle bound for the graph consumer loop. Every producer that
# feeds the event queue is itself bounded — provider streams by
# their idle timeout (120s), tools by their ``timeout_s``, MCP calls
# by their per-call timeout — so a run that produces NOTHING for
# this long is wedged, not working: the consumer raises TimeoutError
# and the task FAILS visibly instead of hanging forever with the UI
# waiting on a run that will never speak again. It is an idle bound,
# not a total deadline: a run that keeps producing events may take
# as long as it takes.
GRAPH_EVENT_IDLE_TIMEOUT_SECONDS = 1800.0

# N-06: grace for a cancelled graph runner to finish unwinding
# while the consumer's teardown awaits it — the N-02 discipline
# (cf. MCP_TOOL_CALL_CANCEL_GRACE_SECONDS). A well-behaved runner
# stops in milliseconds; the grace only bounds how long teardown
# waits on one that doesn't.
_RUNNER_SHUTDOWN_GRACE_SECONDS = 5.0


async def _reap_runner(task: asyncio.Task) -> None:
    """Observe a cancelled graph runner to completion (N-06).

    Fallback for the teardown paths where the consumer's finally
    cannot await the runner itself (notably GeneratorExit teardown,
    where a suspending await is forbidden): the runner's outcome is
    still retrieved — never left as an unobserved task that keeps
    executing after unsubscribe or dies with an unretrieved error.
    """
    with contextlib.suppress(asyncio.CancelledError):
        try:
            await task
        except Exception:
            # Retrieval only: the run's own error was already
            # forwarded to the consumer as graph_error.
            logger.debug("Graph runner ended with an error during reap",
                         exc_info=True)

# ── S12: silent delegation ──────────────────────────────────────────
# Delegation is a state, not chat text (master build S12, audit G3).
# A delegation transition is signalled ONLY by the structured
# NODE_ACTIVITY event built by _delegation_activity_event below —
# never by a "[Supervisor] -> Delegating to X" text line. Before S12
# the run path wrote that line into the episode thinking log and the
# resume path emitted it as a TASK_PROGRESS delta (with literal "\n"
# escapes, so the UI's text filter could not even match it); the UI
# carried two string-sniffing band-aids for what is now structural.
# The supervisor speaks to the user only through its genuine prose,
# the approval channel (TOOL_APPROVAL_REQUEST / TASK_INTERRUPTED) and
# the final answer; while a delegation is outstanding its routing
# rationale travels on the activity event, not in the answer stream.
# Mirrors supervisor_model.HANDOFF_PREFIX (the transfer tool names)
# and supervisor_model.DELEGATION_PREFIX (the P1d delegation tool
# names); defined here because supervisor_model is loaded by the
# graph, not by this module.
_DELEGATION_HANDOFF_PREFIXES = ("transfer_to_", "delegate_to_")

# P2b: the parallel-group tool's result message carries the joined
# member outcomes — it surfaces as response text exactly like a
# delegation tool's result (the two sites below). Mirrors
# fanout.GROUP_TOOL_NAME; defined here for the same reason as the
# prefixes above.
_PARALLEL_GROUP_TOOL_NAME = "delegate_in_parallel"


def _is_delegation_result_message(msg: Any) -> bool:
    """True for a tool message carrying a delegation's result: a
    P1d delegation tool's answer, or a P2b parallel group's joined
    member outcomes."""
    if not isinstance(msg, dict) or msg.get("role") != "tool":
        return False
    name = str(msg.get("name") or "")
    return name.startswith("delegate_to_") or name == _PARALLEL_GROUP_TOOL_NAME


def _is_handoff_message(msg: Any) -> bool:
    """True when a message dict carries a supervisor handoff call.

    Such a message is a routing act, not prose: for native routing
    the supervisor's text alongside the transfer call IS the
    delegation reason (see supervisor_model), and that reason already
    rides the NODE_ACTIVITY payload — so the executor gate keeps the
    message's content (and its delegation "tool call") out of the
    user-facing text stream entirely.
    """
    if not isinstance(msg, dict):
        return False
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        name = name or tc.get("name") or ""
        if str(name).startswith(_DELEGATION_HANDOFF_PREFIXES):
            return True
    return False


def _delegation_activity_event(
    task_id: str,
    request_id: str,
    log_entry: Any,
    *,
    delegation_count: int | None,
    user_prompt: str,
    updated_plan: str,
) -> Event | None:
    """The one structured signal for a delegation transition.

    Built from a fresh ``delegation_log`` entry. Returns None for log
    entries that are not delegations — the finish decision
    (next="END"), supervisor memory steps (next="supervisor") and
    malformed entries — the same exclusion the S4 counter and
    ``delegation_briefing`` apply: those are supervisor bookkeeping,
    and the finish is carried by TASK_COMPLETED, not by a spurious
    "Delegating to END" activity card (pre-S12 behaviour).

    ``delegation_count`` is the S4 authoritative counter read from
    graph state (never recounted here); it rides the payload so the
    activity surface can show how many delegations this turn has
    used. The S3 Task Ledger tap consumes this event type — on both
    executor paths since S12 (the resume path previously surfaced
    delegations as TASK_PROGRESS text, which the tap never read).
    """
    if not isinstance(log_entry, dict):
        return None
    decision = log_entry.get("decision") or {}
    if not isinstance(decision, dict):
        return None
    next_step = decision.get("next")
    if not next_step or str(next_step).upper() in ("END", "SUPERVISOR"):
        return None
    reason = decision.get("reason", "")
    briefing = decision.get("briefing", "")
    denied = bool(decision.get("denied"))
    payload: dict[str, Any] = {
        "task_id": task_id,
        "from_node": "supervisor",
        "to_node": str(next_step).lower(),
        "action": "delegation_denied" if denied else "delegation",
        "reason": reason,
        "briefing": briefing,
        "input_payload": {
            "user_prompt": user_prompt,
        },
        "output_payload": {
            "decision": decision,
            "briefing": briefing if briefing else None,
            "assigned_specialist": str(next_step).lower(),
            "updated_plan": updated_plan,
        },
        "status": "completed",
        "timestamp": int(time.time()),
    }
    if delegation_count is not None:
        payload["delegation_count"] = delegation_count
    return Event(
        type=EventType.NODE_ACTIVITY,
        request_id=request_id,
        payload=payload,
    )


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


def _new_delegation_events(
    *,
    task_id: str,
    request_id: str,
    entries: list,
    delegation_count: int | None,
    user_prompt: str,
    updated_plan: str,
    response_parts: list[str],
) -> list[Event]:
    """Events for newly-seen delegation_log entries (run + resume).

    One NODE_ACTIVITY per delegation transition (S12 — the only
    signal for a delegation; non-delegation entries produce none)
    and, for a denied delegation, the explanation as response text
    (E1-11), appended to ``response_parts`` as well as emitted.

    Every new entry is processed, not only the latest: under the
    legacy topology a supervisor update grows the log by at most
    one entry per hop, but a P1d tools-topology run delivers its
    whole log inside the supervisor node's final update, and every
    transition must still surface.

    ``delegation_count`` is the counter's value at the delivering
    update; each entry's event carries the counter as it stood when
    that entry's delegation executed, reconstructed by walking the
    batch in order (an executed delegation is the only entry kind
    that increments the counter — the S4 rule). For the legacy
    one-entry batches this yields exactly the delivered value.
    """
    def _counted(entry: dict) -> bool:
        decision = entry.get("decision")
        if not isinstance(decision, dict):
            return False
        target = decision.get("next")
        if not target or str(target).upper() == "END" \
                or str(target) == "supervisor":
            return False
        return not decision.get("denied")

    running: int | None = None
    if delegation_count is not None:
        running = delegation_count - sum(
            1 for e in entries if isinstance(e, dict) and _counted(e))
    events: list[Event] = []
    for log_entry in entries:
        if not isinstance(log_entry, dict):
            continue
        decision = log_entry.get("decision", {})
        if running is not None and _counted(log_entry):
            running += 1
        activity = _delegation_activity_event(
            task_id, request_id, log_entry,
            delegation_count=running,
            user_prompt=user_prompt,
            updated_plan=updated_plan,
        )
        if activity is not None:
            events.append(activity)
        if isinstance(decision, dict) and decision.get("denied"):
            denial_text = (
                f"Delegation to {decision.get('next', 'UNKNOWN')} was "
                f"blocked by the spawn policy: {decision.get('reason', '')}"
            )
            response_parts.append(denial_text)
            events.append(Event(
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
    return events


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

    # S3 (master build): the Task Ledger's live tap records every
    # event this path hands its caller (see the emit_event funnel and
    # the terminal yields below; the resume path taps the same way).
    record_executor_event(task_id, Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={"task_id": task_id, "prompt": prompt, "state": state_enum.value,
                 "session_id": session_id},
    ))
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

        with supervisor_pattern_context(
                getattr(kwargs.get("user_config"),
                        "supervisor_pattern", None)):
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

        # E1-F10: enforce the caller's turn budget. max_turns rides
        # the run's recursion limit — LangGraph counts subgraph steps
        # against it, so the budget bounds specialist model/tool
        # turns, not just supervisor hops — while the fixed graph
        # ceiling stays the backstop for budgets at or above it (a
        # smaller requested budget was previously accepted and then
        # ignored entirely). The resume path has no max_turns input
        # and keeps the fixed ceiling.
        if isinstance(max_turns, int) and 0 < max_turns < GRAPH_RECURSION_LIMIT:
            config["recursion_limit"] = max_turns

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
            # S4: the per-turn delegation counter starts at 0 for
            # every new run — the cap is per user turn. The approval
            # resume path never re-seeds state, so a delegation that
            # pauses for approval keeps its count across the pause
            # and is never counted twice.
            "delegation_count": 0,
            # S13: the per-turn sub-agent counter, seeded and
            # checkpointed exactly like delegation_count (separate
            # channel, separate cap — see state.py).
            "subagent_count": 0,
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

        # Calendar layer C2 (14 Nov 2026): chat event extraction —
        # the same hook fast chat uses. If the prompt stated a future
        # event it is already stored; the note rides this run's
        # messages so the reply announces it (with the undo path)
        # rather than the add happening silently. It stays in the
        # thread as a system record of what the calendar did.
        # Best-effort, like skill auto-match above: the hook never
        # raises, and extraction must never break a task start.
        try:
            from aether_engine.calendar.extraction import (
                note_for_message_if_enabled,
            )

            _calendar_note = await asyncio.to_thread(
                note_for_message_if_enabled, prompt)
        except Exception:  # noqa: BLE001
            _calendar_note = None
        if _calendar_note:
            initial_state["messages"] = [
                *initial_state["messages"],
                {"role": "system", "content": _calendar_note},
            ]

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
        # S12: last S4 counter value seen in a state update — read
        # from state, never recounted (the counter itself lives in
        # AetherState and is incremented by the supervisor hook).
        delegation_count_seen = 0
        thinking_log = []
        accumulated_thoughts = []
        # Wave 6 (SPEC-6B C6): the reply's memory_used disclosure,
        # folded from the memory tools' completed TOOL_ACTIVITY
        # events (nodes/supervisor.py attaches the scored items
        # as payload["memory_used"]). One entry per (kind, id),
        # first-seen order; capped when the reply is emitted.
        memory_used_items: list[dict] = []
        memory_used_seen: set = set()

        def record_thought(ev: Event):
            p = ev.payload
            if ev.type == EventType.TOOL_ACTIVITY:
                for entry in p.get("memory_used") or []:
                    key = (entry.get("kind"), entry.get("id"))
                    if key not in memory_used_seen:
                        memory_used_seen.add(key)
                        memory_used_items.append(entry)
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
            # S3: every funnelled event also lands in the Task
            # Ledger's bounded live tap (observational, never raises).
            record_executor_event(task_id, ev)
            return ev

        # N-34: bounded — the bus drops (and logs) when this fills,
        # and the runner's puts backpressure instead of accumulating
        # events in memory behind a stalled consumer.
        event_queue = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
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
                # N-06: idle-bounded — see
                # GRAPH_EVENT_IDLE_TIMEOUT_SECONDS. A TimeoutError
                # here lands in the except below and is reported as
                # TASK_FAILED, like any other graph failure.
                item = await asyncio.wait_for(
                    event_queue.get(),
                    timeout=GRAPH_EVENT_IDLE_TIMEOUT_SECONDS)
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
                            record_executor_event(task_id, Event(
                                type=EventType.TOOL_APPROVAL_REQUEST,
                                request_id=request_id,
                                payload={"tool_name": intr_val.get("tool_name"),
                                         "task_id": task_id},
                            ))
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
"desktop_confirmation": intr_val.get("desktop_confirmation"),
                                    "workspace_roots": workspace_roots or [],
                                },
                            )
                            interrupted = True
                            break
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "supervisor" and isinstance(state_update.get("messages"), list):
                        # P1d (tools topology): a specialist's answer
                        # arrives inside the supervisor node's update,
                        # carried by its delegation tool's result
                        # message — surface it as response text, the
                        # way a specialist node's own update surfaces
                        # it under the legacy topology. (No such
                        # messages exist under the legacy topology.
                        # The scan normalises through message_dicts:
                        # single-message writes inside the agent stay
                        # BaseMessage objects in the update.)
                        for _msg in message_dicts(state_update["messages"])[messages_seen:]:
                            if _is_delegation_result_message(_msg) and _msg.get("content"):
                                full_response_parts.append(_msg["content"])
                                yield emit_event(Event(
                                    type=EventType.TASK_PROGRESS,
                                    request_id=request_id,
                                    payload={
                                        "task_id": task_id,
                                        "text_delta": _msg["content"],
                                        "full_text": _msg["content"],
                                        "turn": 0,
                                        "node": "assistant",
                                    },
                                ))
                        messages_seen = max(messages_seen, len(state_update["messages"]))
                    if isinstance(state_update.get("delegation_count"), int):
                        delegation_count_seen = state_update["delegation_count"]
                    if node_name == "supervisor" and len(state_update.get("delegation_log") or []) > delegation_seen:
                        new_entries = state_update["delegation_log"][delegation_seen:]
                        delegation_seen = len(state_update["delegation_log"])
                        for _delegation_event in _new_delegation_events(
                                task_id=task_id, request_id=request_id,
                                entries=new_entries,
                                delegation_count=delegation_count_seen,
                                user_prompt=prompt,
                                updated_plan=state_update.get("plan", ""),
                                response_parts=full_response_parts):
                            yield emit_event(_delegation_event)
                    elif "messages" in state_update and isinstance(state_update["messages"], list):
                        new_messages = state_update["messages"][messages_seen:]
                        messages_seen = len(state_update["messages"])
                        for msg in new_messages:
                            if msg.get("role") == "assistant":
                                if node_name == "supervisor" and _is_handoff_message(msg):
                                    # S12 gate: a supervisor message
                                    # carrying a handoff call is a
                                    # routing act — its content is the
                                    # delegation reason, already on
                                    # the NODE_ACTIVITY payload. It
                                    # never enters the answer stream
                                    # and its transfer call is not a
                                    # tool activity.
                                    continue
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
                record_executor_event(task_id, Event(
                    type=EventType.TASK_INTERRUPTED,
                    request_id=request_id,
                    payload={"task_id": task_id,
                             "state": TaskState.PENDING_APPROVAL.value,
                             "latency_ms": elapsed_ms,
                             "session_id": session_id},
                ))
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
                # Wave 6 (D-5, SPEC-6B C4): the write gate — with
                # memory_learn_enabled off, a completed task stores
                # nothing (no episode, no facts, no lessons). This
                # is the supervisor path's learn seam; the resume
                # completion below gates the same way.
                if full_response and getattr(
                        kwargs.get("user_config"),
                        "memory_learn_enabled", True):
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

                record_executor_event(task_id, Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=request_id,
                    payload={"task_id": task_id,
                             "state": TaskState.SUCCEEDED.value,
                             "latency_ms": elapsed_ms,
                             "session_id": session_id},
                ))
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
                        # SPEC-6B C6: the memory items this reply's
                        # context actually used, capped at 5 (empty
                        # when memory was off or nothing scored).
                        "memory_used": memory_used_items[:5],
                    },
                )
        except Exception as e:
            logger.exception("LangGraph task failed")
            elapsed_ms = round((time.time() - start_time) * 1000, 2) if 'start_time' in locals() else 0.0
            logger.exception(f"Graph execution failed for task {task_id}")
            record_executor_event(task_id, Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={"task_id": task_id,
                         "state": TaskState.FAILED.value,
                         "error": str(e),
                         "latency_ms": elapsed_ms},
            ))
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
            # N-06: cancel the runner AND await its wind-down. The
            # old code only cancelled: an abandoned run's runner kept
            # executing provider/tool work after unsubscribe, and its
            # outcome was never retrieved. The wait is shielded and
            # graced (a stubborn runner cannot hold teardown hostage);
            # where even that await is impossible, _reap_runner
            # observes the runner instead — it is never left running
            # unobserved either way.
            runner_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(runner_task),
                    timeout=_RUNNER_SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                logger.warning(
                    "Graph runner for task %s did not stop within "
                    "%.0fs of cancellation",
                    task_id, _RUNNER_SHUTDOWN_GRACE_SECONDS)
            except asyncio.CancelledError:
                # Our own cancellation arrived during the wait: hand
                # the still-finishing runner to the reaper, then let
                # our cancellation proceed — unless the CancelledError
                # is the runner's own surfacing through the shield,
                # which is the expected end of a cancelled runner.
                if not runner_task.done():
                    asyncio.ensure_future(_reap_runner(runner_task))
                if not runner_task.cancelled():
                    raise
            except RuntimeError:
                # GeneratorExit (aclose) teardown: a suspending await
                # is forbidden there, so the reap below is the only
                # observation this path can make — the cancellation
                # above has still been delivered.
                pass
            if not runner_task.done():
                asyncio.ensure_future(_reap_runner(runner_task))

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
    desktop_envelope: dict[str, Any] | None = None,
    desktop_remember: bool = False,
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

    The continuation machinery lives in ``_resume_thread_impl``;
    this entry is the approval-shaped one — its first segment
    delivers the owner's decision to the pending interrupt.
    """
    # Epic 8, E8-1.3: approval-time argument edits ride the
    # resume value as a dict; an unedited approval keeps the
    # legacy bare-boolean resume exactly as before.
    # Epic 8, E8-6.2: a browser-envelope grant granted at a
    # confirmation rides the same dict (the pause node scopes
    # it to the confirmed action's category + site itself).
    # S20: the desktop extras (per-task envelope grant +
    # remembered-grant flag) ride it the same way.
    resume_value: Any = approved
    if approved and (isinstance(edited_args, dict)
                     or isinstance(browser_envelope, dict)
                     or isinstance(desktop_envelope, dict)
                     or desktop_remember):
        resume_value = {"approved": True}
        if isinstance(edited_args, dict):
            resume_value["edited_args"] = edited_args
        if isinstance(browser_envelope, dict):
            resume_value["browser_envelope"] = browser_envelope
        if isinstance(desktop_envelope, dict):
            resume_value["desktop_envelope"] = desktop_envelope
        if desktop_remember:
            resume_value["desktop_remember"] = True
    async for event in _resume_thread_impl(
            thread_id, Command(resume=resume_value), provider,
            tools=tools, workspace_roots=workspace_roots,
            disabled_nodes=disabled_nodes, agent_models=agent_models,
            agent_tools=agent_tools, **kwargs):
        yield event


async def continue_langgraph_task(
    thread_id: str,
    provider: BaseProvider,
    tools: ToolRegistry | None = None,
    workspace_roots: list[str] | None = None,
    disabled_nodes: set | None = None,
    agent_models: dict[str, str] | None = None,
    agent_tools: dict[str, list[str]] | None = None,
    **kwargs,
) -> AsyncGenerator[Event, None]:
    """Plain continuation of an interrupted thread (Wave 6, D-1).

    The same continuation machinery as ``resume_langgraph_task``
    with one difference that matters: the first segment consumes
    ``None``. There is no pending interrupt to answer — the task
    was interrupted by a restart, not paused for approval — so
    no decision is delivered, and none CAN be consumed by the
    first fresh approval interrupt the continued run raises:
    every gated call in the continued run re-gates and asks,
    exactly as D-1 requires. Callers must already have
    established (via the resume audit) that no approval is
    pending on the thread.
    """
    async for event in _resume_thread_impl(
            thread_id, None, provider,
            tools=tools, workspace_roots=workspace_roots,
            disabled_nodes=disabled_nodes, agent_models=agent_models,
            agent_tools=agent_tools, **kwargs):
        yield event


async def _resume_thread_impl(
    thread_id: str,
    first_segment_input: Any,
    provider: BaseProvider,
    tools: ToolRegistry | None = None,
    workspace_roots: list[str] | None = None,
    disabled_nodes: set | None = None,
    agent_models: dict[str, str] | None = None,
    agent_tools: dict[str, list[str]] | None = None,
    **kwargs,
) -> AsyncGenerator[Event, None]:
    """Shared continuation machinery for the two entries above.

    ``first_segment_input`` is the ONLY difference between them:
    a ``Command(resume=...)`` for the approval resume, ``None``
    for the plain continuation. Everything else — the M17
    routing-constraint re-derivation, E1-F04 snapshot restore,
    E1-F16 bus subscription, manifest seeding, steering drain,
    completion/failure translation — is identical for both, so
    it lives exactly once, here.
    """
    _local_only = kwargs.get("local_only")
    if _local_only is None:
        from aether_engine.routing.policy import local_only_from_vision_override

        _local_only = local_only_from_vision_override(
            getattr(kwargs.get("user_config"), "auxiliary_models", None))
    db_path = DEFAULT_CHECKPOINT_DB
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        with supervisor_pattern_context(
                getattr(kwargs.get("user_config"),
                        "supervisor_pattern", None)):
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
        bus_queue: asyncio.Queue | None = None

        try:
            state_obj = await graph.aget_state(config)
            task_id = state_obj.values.get("task_id", thread_id)
            # E1-F16: subscribe to the task's event bus for the whole
            # resume, as the run path does. Specialists publish
            # THINKING on the bus and execute_tool publishes
            # SCRATCHPAD; this path never subscribed, so anything a
            # resumed specialist thought or rendered simply vanished.
            # _consume_segment merges the bus queue into each segment.
            bus_queue = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
            event_bus.subscribe(task_id, bus_queue)
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
            # S12: the S4 counter resumes from its checkpointed
            # value (None on a pre-S4 checkpoint — the activity
            # payload then omits the count rather than inventing one).
            delegation_count_seen = state_obj.values.get("delegation_count")

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
                nonlocal interrupted, messages_seen, delegation_seen, delegation_count_seen

                # E1-F16: the segment's graph updates merged with
                # the task's bus queue — merge_segment_items (in
                # langgraph.streaming) is that merge; the graph
                # translation below is this path's own and stays.
                async for kind, data in merge_segment_items(
                        graph, segment_input, config, bus_queue):
                    if kind == "bus":
                        yield data
                        continue
                    event = data
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
"desktop_confirmation": intr_val.get("desktop_confirmation"),
                                        "workspace_roots": workspace_roots or [],
                                    },
                                )
                                interrupted = True
                                break
                        if not isinstance(state_update, dict):
                            continue
                        if node_name == "supervisor" and isinstance(state_update.get("messages"), list):
                            # P1d (tools topology): as on the run
                            # path, a specialist's answer arrives
                            # inside the supervisor node's update on
                            # its delegation tool's result message —
                            # surface it as response text. (No such
                            # messages exist under the legacy
                            # topology.)
                            for _msg in message_dicts(state_update["messages"])[messages_seen:]:
                                if _is_delegation_result_message(_msg) and _msg.get("content"):
                                    full_response_parts.append(_msg["content"])
                                    yield Event(
                                        type=EventType.TASK_PROGRESS,
                                        request_id=thread_id,
                                        payload={
                                            "task_id": task_id,
                                            "text_delta": _msg["content"],
                                            "full_text": _msg["content"],
                                            "turn": 0,
                                            "node": "assistant",
                                        },
                                    )
                            messages_seen = max(messages_seen, len(state_update["messages"]))
                        if isinstance(state_update.get("delegation_count"), int):
                            delegation_count_seen = state_update["delegation_count"]
                        if node_name == "supervisor" and len(state_update.get("delegation_log") or []) > delegation_seen:
                            # S12: resume delegations are signalled by
                            # the same structured NODE_ACTIVITY as the
                            # run path, via _new_delegation_events —
                            # which also carries the E1-11 denial
                            # surfacing (the explanation is the
                            # resumed task's response when no
                            # specialist runs).
                            new_entries = state_update["delegation_log"][delegation_seen:]
                            delegation_seen = len(state_update["delegation_log"])
                            for _delegation_event in _new_delegation_events(
                                    task_id=task_id, request_id=thread_id,
                                    entries=new_entries,
                                    delegation_count=delegation_count_seen,
                                    user_prompt=state_obj.values.get(
                                        "original_prompt", ""),
                                    updated_plan=state_update.get("plan", ""),
                                    response_parts=full_response_parts):
                                yield _delegation_event
                        elif "messages" in state_update and isinstance(state_update["messages"], list):
                            new_messages = state_update["messages"][messages_seen:]
                            messages_seen = len(state_update["messages"])
                            for msg in new_messages:
                                if node_name == "supervisor" and _is_handoff_message(msg):
                                    # S12 gate (same as the run path):
                                    # a handoff message's content is
                                    # the delegation reason and rides
                                    # the NODE_ACTIVITY payload — it is
                                    # not answer text.
                                    continue
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
            # Wave 6 (SPEC-6B C6): the resumed reply's memory_used
            # disclosure, folded from the memory tools' completed
            # TOOL_ACTIVITY events exactly as the run path folds
            # them in record_thought — one entry per (kind, id).
            memory_used_items: list[dict] = []
            memory_used_seen: set = set()

            def _note_memory_used(_ev: Event) -> None:
                if _ev.type != EventType.TOOL_ACTIVITY:
                    return
                for entry in _ev.payload.get("memory_used") or []:
                    key = (entry.get("kind"), entry.get("id"))
                    if key not in memory_used_seen:
                        memory_used_seen.add(key)
                        memory_used_items.append(entry)

            # S3: the resume path's translated events tap into the
            # Task Ledger here, at the segment funnel — the mirror of
            # the run path's emit_event tap.
            async for _ev in _consume_segment(first_segment_input):
                _note_memory_used(_ev)
                record_executor_event(task_id, _ev)
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
                    _note_memory_used(_ev)
                    record_executor_event(task_id, _ev)
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
                record_executor_event(task_id, Event(
                    type=EventType.TASK_INTERRUPTED,
                    request_id=thread_id,
                    payload={"task_id": task_id,
                             "state": TaskState.PENDING_APPROVAL.value,
                             "latency_ms": elapsed_ms,
                             "session_id": state_obj.values.get("session_id")},
                ))
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
                # Wave 6 (D-5, SPEC-6B C4): the write gate, same as
                # the fresh-completion path — memory_learn_enabled
                # off stores nothing for a resumed task either.
                _resume_user_config = config["configurable"].get("user_config")
                memory_backend = backend_or_none(
                    _resume_user_config, context="resumed task memory") \
                    if getattr(_resume_user_config,
                               "memory_learn_enabled", True) else None
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

                record_executor_event(task_id, Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=thread_id,
                    payload={"task_id": task_id,
                             "state": TaskState.SUCCEEDED.value,
                             "latency_ms": elapsed_ms,
                             "session_id": state_obj.values.get("session_id")},
                ))
                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=thread_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.SUCCEEDED.value,
                        "latency_ms": elapsed_ms,
                        "response": full_response or "(task completed)",
                        "session_id": state_obj.values.get("session_id"),
                        # SPEC-6B C6: as the run path's reply —
                        # the memory items this resumed reply's
                        # context actually used, capped at 5.
                        "memory_used": memory_used_items[:5],
                    },
                )
        except Exception as e:
            logger.exception("LangGraph task failed during resume")
            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            retriable = _is_retriable_error(e)
            record_executor_event(task_id, Event(
                type=EventType.TASK_FAILED,
                request_id=thread_id,
                payload={"task_id": task_id,
                         "state": TaskState.FAILED.value,
                         "error": str(e),
                         "latency_ms": elapsed_ms},
            ))
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
        finally:
            # E1-F16: the bus subscription ends with the resume,
            # however it ended (bus_queue is only set once the
            # subscription above actually happened).
            if bus_queue is not None:
                event_bus.unsubscribe(task_id, bus_queue)

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
                                "browser_confirmation": intr_val.get("browser_confirmation"),
                                "desktop_confirmation": intr_val.get("desktop_confirmation")
                            })
                            break
            except Exception as e:  # noqa: BLE001 — per-thread scan isolation: one unreadable thread is logged and must not abort the approvals listing
                logger.warning(f"Error checking thread {t_id}: {e}")
                
    return pending
