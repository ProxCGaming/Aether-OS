"""Subagents-as-tools supervisor (P1d): the delegation tools and the
graph builder that replaces ``create_supervisor``.

The supervisor is a plain ``create_react_agent`` over ``AetherState``
— the same agent construction ``create_supervisor`` used internally,
with the same model adapter, routing prompt and post-model hook —
whose delegation tools each invoke one specialist's existing compiled
subgraph and return its result as a tool result. The supervisor then
decides the next step (delegate on, or finish) exactly as the
post-specialist decision worked under the handoff topology.

Why the library goes: ``langgraph-supervisor`` is unmaintained
(0.0.31 is its last release) and LangChain's migration guide directs
users to this pattern. The Step 0 spike (``spike_p1d/`` in the build
worktree; verdict recorded there) proved the two mechanics the
pattern stands on, on the pinned langgraph 1.2.16:

- an approval ``interrupt()`` raised inside a specialist invoked
  from a delegation tool bubbles to the outer graph, and resuming
  the outer graph re-enters the tool and resumes into the same
  specialist — with the specialists compiled with the shared
  checkpointer, exactly as ``_build_specialist_graph`` builds them
  today;
- the specialist receives the full conversation plus the
  ``context_briefing`` the post-model hook wrote immediately before
  the tool ran, and its full message trail lands back in the
  supervisor's history (``full_history`` semantics).

One framework constraint shapes the tool's return: Aether's
``collapse_subgraph_echo`` reducer converts any multi-message
channel write to plain dicts, and the agent's chat-history
validation only recognises ``BaseMessage`` instances — a
``ToolMessage`` batched with the specialist's dict messages would
become a dict and the delegation call would read as unanswered.
The tool therefore returns two ``Command``s: the tool result first
(a single-message write, which stays a ``ToolMessage``), then the
specialist's message delta and merged state channels.

P1e mode + cap enforcement (K-04 hookup) also lives at this seam —
the tools topology only; the legacy graph never imports this
module, so its behaviour is untouched:

- **Mode ``off``:** the delegation tools are not offered (the
  supervisor model adapter filters them from the offer) and a
  call that still arrives — a forged or stale pending call, or a
  JSON decision the adapter synthesised — is refused here with
  the mode named; the specialist never starts. The graph builder
  receives no per-run config (its signature is a frozen seam), so
  the tools stay *bound*; off is enforced at the offer seam plus
  this refusal, the S19 bound-but-never-offered precedent.
- **Mode ``manual``:** every delegation pauses for owner approval
  via ``interrupt()`` BEFORE the specialist is invoked — the same
  approval surface (payload type ``approval_request``, bool or
  ``{"approved": ...}`` resume value) as a specialist's tool
  approval, with the payload naming the specialist and briefing.
  A denial returns the refusal as the tool result; the specialist
  never runs.
- **Per-delegation caps:** ``per_delegation_timeout_s`` and
  ``per_delegation_token_budget`` (0 = disabled = today's
  behaviour, a bare ``ainvoke``) are enforced by a watchdog around
  the subgraph invoke: the wall-clock deadline bounds each
  continuous execution segment (an approval pause unwinds the
  invoke, so owner wait time is never charged — the S13 rule),
  and the token budget is judged from UsageLedger rows for the
  task recorded during the delegation window (the delta over a
  baseline snapshotted at delegation start, so earlier spend —
  the supervisor's own calls included — never counts). A stop
  cancels the invoke and returns the honest stop text as the
  tool result; partial specialist work is abandoned, exactly as
  an S13 budget stop abandons the child's. The ledger only sees
  what the provider layer records, so a provider built with
  recording off leaves the timeout as the binding bound — the
  same honest limit S13 states. ``per_delegation_cost_budget_usd``
  is NOT enforced here: supervisor delegations carry no
  per-delegation cost attribution (their calls record under the
  run's task class, not a per-delegation marker), and it stays
  enforced at the S13 spawn seam, where the marker exists.
"""

import asyncio
import logging
import time
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolRuntime, create_react_agent
from langgraph.types import Command, interrupt

from aether_engine.cost.ledger import get_usage_ledger
from aether_engine.delegation_settings import (
    DelegationSettings,
    resolve_delegation_settings,
)
from aether_engine.langgraph.fanout import build_parallel_group_tool
from aether_engine.langgraph.nodes.supervisor import (
    build_supervisor_desktop_tools,
    build_supervisor_memory_tools,
)
from aether_engine.langgraph.state import AetherState, message_dicts
from aether_engine.langgraph.supervisor_model import (
    DELEGATION_PREFIX,
    AetherSupervisorModel,
    supervisor_post_model_hook,
    supervisor_routing_prompt,
)

logger = logging.getLogger("aether_engine.langgraph.delegation")

# State channels a specialist subgraph may write, merged back into
# the supervisor's state when a delegation returns. The specialist's
# final state derives from the very state the tool was invoked with,
# so echo-valued writes are no-ops and appended channels merge
# through their reducers exactly as a subgraph node's output did.
# Deliberately NOT merged: ``delegation_count`` (owned by the
# post-model hook — the single increment), ``context_briefing``
# (hook-owned), the run's identity/config channels (task_id,
# session_id, run_config, plan_mode, active_skills, message_id,
# original_prompt) and the managed ``remaining_steps``.
MERGED_CHANNELS = (
    "blackboard",
    "delegation_log",
    "plan",
    "current_phase",
    "artifact_paths",
    "tool_results_summary",
    "pending_tool_call",
    "queued_tool_calls",
    "tool_call_signature",
    "tool_call_repeats",
    "write_tool_used",
    "structured_outputs",
    "subagent_count",
    "context_summary",
    "context_summary_upto",
)


def _final_answer_text(messages: list) -> str:
    """The specialist's final answer in a message delta: the last
    assistant message with content (the same derivation the
    supervisor adapter applies to its post-specialist view)."""
    for msg in reversed(message_dicts(messages)):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


# ── P1e mode + cap enforcement (tools topology) ───────────────────

#: Watchdog poll interval for the per-delegation caps. Only used
#: when a cap is enabled; the defaults (both caps 0) never poll.
_CAP_POLL_S = 0.25


def delegation_settings_for(config: Any) -> DelegationSettings:
    """The resolved P1e settings for a run config (the read path)."""
    configurable = (config or {}).get("configurable", {}) or {}
    return resolve_delegation_settings(configurable.get("user_config"))


def approval_granted(decision: Any) -> bool:
    """Interpret an approval resume value (pause.py's convention:
    the legacy boolean, or ``{"approved": bool, ...}``)."""
    if isinstance(decision, dict):
        return bool(decision.get("approved"))
    return bool(decision)


def _subject(specialist: str) -> str:
    """How a delegation target reads in prose: a specialist name
    becomes "the researcher specialist"; the fan-out group passes
    its own full subject ("the parallel group (a + b)")."""
    if specialist.startswith("the "):
        return specialist
    return f"the {specialist} specialist"


def mode_off_refusal(specialist: str) -> str:
    """The tool result for a delegation attempted in mode ``off``."""
    return (
        f"Delegation refused: the delegation mode is 'off' for "
        f"this run, so {_subject(specialist)} was not started. "
        f"Delegation is disabled — do the work yourself or answer "
        f"directly, and tell the user delegation is off if the "
        f"task needs a specialist.")


def approval_denial_text(specialist: str) -> str:
    """The tool result when the owner declines a manual-mode
    delegation (the spawn-policy denial's surface, as a tool
    result): the specialist never ran, and the supervisor is told
    why and what to do instead."""
    return (
        f"Delegation not approved: the user declined the "
        f"delegation to {_subject(specialist)}, so it was not "
        f"run. Do not retry this delegation unless the user asks "
        f"for it — continue without it, or ask the user how they "
        f"would like the work handled.")


def delegation_approval_payload(
        specialist: str, briefing: str, *,
        tool_name: str, tool_call_id: Any, config: Any) -> dict:
    """The manual-mode approval interrupt payload.

    The house approval shape (``type: "approval_request"`` — the
    executor surfaces exactly those as pending approvals), with
    the delegation named explicitly: ``tool_name`` is the
    delegation tool, ``args`` carries the briefing, and the
    ``delegation`` field names the specialist and briefing for
    surfaces that render delegations natively.
    """
    configurable = (config or {}).get("configurable", {}) or {}
    return {
        "type": "approval_request",
        "tool_name": tool_name,
        "args": {"briefing": briefing},
        "request_id": tool_call_id,
        "task_class": configurable.get("task_class", "standard"),
        "delegation": {"specialist": specialist, "briefing": briefing},
    }


def _task_tokens_total(task_id: Any) -> int:
    """Total tokens the UsageLedger recorded for a task (0 when
    unobservable). Never raises — the S13 usage-read discipline:
    a ledger failure reports 0 and the timeout still binds."""
    if not task_id:
        return 0
    try:
        data = get_usage_ledger().query(task_id=task_id)
        return sum(
            int(entry.get("total_tokens") or 0)
            for entry in data.get("entries") or [])
    except Exception:  # observational read; see docstring.
        logger.debug("Delegation usage read failed", exc_info=True)
        return 0


def _delegation_stop_text(specialist: str, kind: str,
                          settings: DelegationSettings,
                          tokens: int) -> str:
    """The honest stop text for a capped delegation (S13's style):
    what was stopped, which bound, and what was observed."""
    if kind == "token_budget":
        return (
            f"Delegation stopped: the {specialist} specialist "
            f"exceeded the per-delegation token budget "
            f"({settings.per_delegation_token_budget:,} tokens) — "
            f"{tokens:,} tokens were recorded in the usage ledger "
            f"for this task during the delegation. The specialist "
            f"was stopped and produced no result; its partial work "
            f"is abandoned. Split the work into smaller "
            f"delegations, or ask the user to raise "
            f"per_delegation_token_budget.")
    return (
        f"Delegation stopped: the {specialist} specialist "
        f"exceeded the per-delegation time budget "
        f"({settings.per_delegation_timeout_s}s). The specialist "
        f"was stopped and produced no result; its partial work "
        f"is abandoned. Split the work into smaller delegations, "
        f"or ask the user to raise per_delegation_timeout_s.")


async def _invoke_specialist(
        specialist: str, specialist_graph, sub_input: dict,
        config: Any, settings: DelegationSettings, *,
        task_id: Any) -> tuple[Any, str | None]:
    """Invoke one specialist subgraph under the per-delegation caps.

    Returns ``(final_state, None)`` on completion, or
    ``(None, stop_text)`` when a cap stopped the delegation. With
    both caps disabled this is a bare ``ainvoke`` — byte-identical
    to the pre-P1e tool. An approval ``GraphInterrupt`` inside the
    specialist propagates untouched, exactly as before: the pause
    unwinds the invoke, so the deadline and the token window span
    only active execution, and the resume re-enters the tool and
    starts a fresh segment.
    """
    timeout_s = settings.per_delegation_timeout_s
    token_budget = settings.per_delegation_token_budget
    if timeout_s <= 0 and token_budget <= 0:
        return await specialist_graph.ainvoke(sub_input, config=config), None

    baseline = _task_tokens_total(task_id) if token_budget > 0 else 0
    invoke = asyncio.ensure_future(
        specialist_graph.ainvoke(sub_input, config=config))
    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    stop: tuple[str, int] | None = None
    try:
        while True:
            if invoke.done():
                return invoke.result(), None
            remaining = (
                None if deadline is None
                else deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                stop = ("timeout", 0)
                break
            wait = _CAP_POLL_S if remaining is None else min(
                _CAP_POLL_S, remaining)
            await asyncio.wait({invoke}, timeout=wait)
            if invoke.done():
                continue
            if token_budget > 0:
                tokens = (
                    await asyncio.to_thread(_task_tokens_total, task_id)
                    - baseline)
                if tokens >= token_budget:
                    stop = ("token_budget", tokens)
                    break
    except asyncio.CancelledError:
        # The run itself is being cancelled: take the specialist
        # down with it (no orphan work) and let the cancellation
        # propagate.
        invoke.cancel()
        raise
    # A cap fired: cancel the invoke and absorb its unwind (a
    # racing approval pause loses to the stop — an over-budget
    # delegation stops, it does not pause, the S13 rule).
    invoke.cancel()
    try:
        await invoke
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
    except Exception:  # the stop already decided the outcome.
        logger.debug(
            "Specialist unwind after a cap stop raised", exc_info=True)
    kind, tokens = stop
    logger.info(
        "Delegation to %s stopped by the per-delegation %s cap",
        specialist, kind)
    return None, _delegation_stop_text(specialist, kind, settings, tokens)


def _make_delegation_tool(name: str, specialist_graph):
    """One delegation tool for one specialist subgraph."""

    @tool(
        f"{DELEGATION_PREFIX}{name}",
        description=(
            f"Delegate a task to the {name} specialist and return its "
            "result. The specialist receives the full conversation and "
            "the briefing; it runs its own tools under the usual "
            "approval gates. 'briefing' carries ONLY facts you "
            "actually retrieved from memory tools for this delegation "
            "(empty string when you retrieved none)."
        ),
    )
    async def _delegate(briefing: str, runtime: ToolRuntime) -> list[Command]:
        state = dict(runtime.state or {})
        # P1e enforcement (see the module docstring): the mode
        # gates run before the specialist is invoked. In mode
        # "off" the tool is never offered, so a call arriving here
        # is forged or stale — refused, never executed. In mode
        # "manual" the delegation pauses for the owner's approval;
        # on resume this function re-enters and interrupt() returns
        # the recorded decision (a later specialist pause re-enters
        # below the gate, so the owner is asked exactly once per
        # delegation).
        settings = delegation_settings_for(runtime.config)
        if settings.effective_mode == "off":
            return [Command(update={"messages": [ToolMessage(
                content=mode_off_refusal(name),
                name=f"{DELEGATION_PREFIX}{name}",
                tool_call_id=runtime.tool_call_id,
            )]})]
        if settings.effective_mode == "manual":
            decision = interrupt(delegation_approval_payload(
                name, briefing,
                tool_name=f"{DELEGATION_PREFIX}{name}",
                tool_call_id=runtime.tool_call_id,
                config=runtime.config))
            if not approval_granted(decision):
                return [Command(update={"messages": [ToolMessage(
                    content=approval_denial_text(name),
                    name=f"{DELEGATION_PREFIX}{name}",
                    tool_call_id=runtime.tool_call_id,
                )]})]
        # remaining_steps is a managed value: the runtime owns it,
        # and it is not a legal subgraph input.
        sub_input = {k: v for k, v in state.items() if k != "remaining_steps"}
        # An approval interrupt inside the specialist raises
        # GraphInterrupt out of this invoke; it must propagate
        # untouched — the outer graph surfaces it as the run's
        # interrupt, and the resume re-enters this tool and resumes
        # the subgraph into the same specialist (spike-proven).
        final, stop_text = await _invoke_specialist(
            name, specialist_graph, sub_input, runtime.config, settings,
            task_id=state.get("task_id"))
        if stop_text is not None:
            return [Command(update={"messages": [ToolMessage(
                content=stop_text,
                name=f"{DELEGATION_PREFIX}{name}",
                tool_call_id=runtime.tool_call_id,
            )]})]

        # The specialist's message delta: what its run appended to
        # the history it was invoked with. Compared in dict space —
        # the subgraph stores the input batch dict-converted (the
        # channel reducer's boundary rule), so an object-space
        # prefix check against the agent's live messages would miss
        # and re-append the whole history.
        input_dicts = message_dicts(state.get("messages") or [])
        final_dicts = message_dicts(final.get("messages") or [])
        prefix = 0
        for have, want in zip(final_dicts, input_dicts):
            if have != want:
                break
            prefix += 1
        if prefix < len(input_dicts):
            logger.warning(
                "Delegation to %s: specialist history does not extend "
                "the input history (common prefix %d of %d); merging "
                "the unmatched tail only.",
                name, prefix, len(input_dicts))
        delta = final_dicts[prefix:]
        tool_message = ToolMessage(
            content=_final_answer_text(delta) or f"The {name} specialist finished.",
            name=f"{DELEGATION_PREFIX}{name}",
            tool_call_id=runtime.tool_call_id,
        )
        channels: dict[str, Any] = {}
        for key in MERGED_CHANNELS:
            if key in final:
                channels[key] = final[key]
        return [
            Command(update={"messages": [tool_message]}),
            Command(update={"messages": delta, **channels}),
        ]

    return _delegate


def build_delegation_tools(specialist_graphs: dict) -> list:
    """One delegation tool per specialist, in registry order."""
    return [
        _make_delegation_tool(name, specialist_graph)
        for name, specialist_graph in specialist_graphs.items()
    ]


def create_tools_supervisor_graph(checkpointer, build_specialist_graph,
                                  specialist_nodes):
    """Build the (uncompiled) subagents-as-tools supervisor workflow.

    Mirrors ``create_supervisor``'s construction with the library
    removed: the specialists are compiled by the caller's builder
    (shared checkpointer, unchanged), and the supervisor agent —
    same model adapter, routing prompt, post-model hook and state
    schema as the legacy graph — is the outer graph's only node;
    delegation happens inside its tools, not via sibling nodes.
    """
    specialist_graphs = {
        name: build_specialist_graph(name, node_fn, checkpointer)
        for name, node_fn in specialist_nodes.items()
    }
    supervisor_agent = create_react_agent(
        model=AetherSupervisorModel(),
        tools=(
            build_supervisor_memory_tools()
            + build_supervisor_desktop_tools()
            + build_delegation_tools(specialist_graphs)
            # P2b: the parallel-group tool rides beside the
            # delegation tools (tools topology only — the legacy
            # graph binds no group tool, so no group can be
            # declared there). Its members run as isolated
            # S13-pattern children, never as these shared
            # subgraphs; see langgraph/fanout.py.
            + [build_parallel_group_tool()]
        ),
        prompt=supervisor_routing_prompt,
        state_schema=AetherState,
        post_model_hook=supervisor_post_model_hook,
        name="supervisor",
    )
    builder = StateGraph(AetherState)
    builder.add_node("supervisor", supervisor_agent)
    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", END)
    return builder
