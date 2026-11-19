"""Bounded sub-agent spawning (master build S13; audit G1 + G2).

Before S13, delegation was exactly depth 1: the supervisor handed off
to a specialist and that was the end of the tree — a specialist facing
a well-scoped side quest (one more search, one small file, one
verification pass) had to do it inline, in its own context, however
tangential it was. And while supervisor handoffs were capped
(E1-11 / S4), no per-delegation budget existed anywhere: cost and
token spend were recorded in the UsageLedger but nothing inside a
task could act on them (the daily budget only raised a soft alert).

S13 gives specialists a spawn seam modelled on
``supervisor_model._spawn_policy_denial`` — allow-list thinking, a
per-turn cap, a last-moment backstop and a logged, surfaced denial —
with every bound enforced in code:

* **Mechanism.** ``run_specialist`` offers specialists one extra
  tool, ``spawn_subagent`` (never in a child run — see depth below).
  Its pending call is intercepted in ``execute_tool_node`` and lands
  here. A spawn runs a REAL specialist graph — the same
  ``_build_specialist_graph`` the supervisor's subgraphs use (same
  specialist node body via ``run_specialist``, same ``tool_router``,
  same approval pause node, same ``execute_tool_node`` and therefore
  the same policy -> approval -> sandbox pipeline) — compiled per
  spawn on its own checkpoint thread in a sibling database
  (``~/.aether/subagent_checkpoints.db``) so child pauses are as
  durable as parent ones.
* **Approvals bridge, they do not bypass.** When the child pauses
  for approval, this seam re-raises the child's own interrupt
  payload through ``interrupt()`` in the parent node: the parent
  graph pauses with the identical payload shape the executor and UI
  already handle, and on resume the decision is forwarded into the
  child via ``Command(resume=...)``. Policy inputs (tool policies,
  path rules, approval mode, the approval-cache grants) are the
  parent's own — there is no second, weaker pipeline.
* **Depth model.** supervisor -> specialist -> sub-agent. Max depth
  1 below specialists, enforced by construction: a child run is
  flagged (``is_subagent_run``) and its tool surface is built
  without the spawn tool, and the interception itself refuses to
  fire inside a child run — a hallucinated spawn call in a child
  meets the ordinary per-agent allow-list refusal instead.
* **Tool surface.** Read-only tools by default (intersected with
  what the parent specialist itself has this run). A spawn may ask
  for more by name; anything the parent does not have is a denial,
  never a silent clamp. In plan mode spawns are disabled outright —
  spawning is an execution act and plan mode is plan-only, the same
  stance the supervisor spawn policy takes (the tool is not offered
  there, and the seam backstops a forged call).
* **Bounds** (config-backed, Settings -> Agent Orchestration):
  ``subagents_enabled`` (default on — the programme is authorised;
  the caps are the control), ``subagents_max_per_turn`` (default 4;
  a SEPARATE counter from S4's ``delegation_count`` — state channel
  ``subagent_count``), ``subagent_timeout_s`` (default 120, a
  wall-clock deadline spanning approval segments: approval WAIT time
  is the user's and is not charged), ``subagent_token_budget``
  (default 20,000) and ``subagent_cost_budget_usd`` (default 0 =
  off). Budgets are enforced from UsageLedger rows: the child's
  provider calls are recorded under a per-spawn task-class marker,
  and spend is checked before every child model call and before any
  approval is surfaced. Honest limit: the ledger only sees what the
  provider layer records — the production provider always records
  (exact counts when reported, estimator-labelled otherwise), but a
  provider built with recording off leaves no rows, and then the
  timeout and the caps are the binding bounds.
* **Failure honesty.** A child that errors is retried ONCE within
  its time budget (fresh child thread; denials, timeouts and budget
  stops never retry). Since S14 the retry is also the child's
  escalation point: it carries a step-up flag, so under auto model
  routing the second attempt runs one tier above its own routing
  decision (a pinned role model is never stepped up). Whatever happens — completion, failure,
  timeout, budget stop, denial — the parent specialist receives a
  bounded summary as the tool result and a digest line lands on the
  blackboard (the supervisor reads it through the existing
  blackboard briefing). Cancelling the parent task cancels the
  awaited child run with it: no orphan writes after the turn ends.
* **Reporting.** Spawns publish S12-shaped ``NODE_ACTIVITY`` events
  (``subagent_spawn`` / ``subagent_completed`` / ``subagent_failed``
  / ``subagent_denied``) on the task's event bus — no new event
  type — and the S3 Task Ledger tap folds them into the parent
  task's ``subagents`` records, so child work is attributable to
  its parent-of-record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

from aether_common.contracts import Event, EventType
from aether_engine.cost.ledger import get_usage_ledger, usage_context
from aether_engine.delegation_settings import resolve_delegation_settings
from aether_engine.event_bus import event_bus

logger = logging.getLogger("aether_engine.langgraph.subagents")

SPAWN_TOOL_NAME = "spawn_subagent"

#: The only roles a sub-agent may take — the existing specialists.
SPECIALIST_ROLES = ("researcher", "planner", "coder")

#: Child checkpoints live in a sibling database, not the parent's:
#: a spawn opens its own saver connection per segment, and a separate
#: file keeps it out of the parent saver's lock domain entirely.
SUBAGENT_CHECKPOINT_DB = Path.home() / ".aether" / "subagent_checkpoints.db"

#: The child graph's own step ceiling (the parent's is 25 too, via
#: GRAPH_RECURSION_LIMIT): bounds tool-loop rounds inside one spawn
#: independently of the wall-clock and token budgets.
CHILD_RECURSION_LIMIT = 25

#: Bounds on what a spawn reports back (the tool result the parent
#: reads, and the excerpts inside it).
SUMMARY_MAX_CHARS = 1200
RESULT_EXCERPT_CHARS = 800
TASK_EXCERPT_CHARS = 200
DETAIL_MAX_CHARS = 300

#: Child-run stop markers (state channel ``subagent_stop``).
STOP_TOKEN_BUDGET = "token_budget"
STOP_COST_BUDGET = "cost_budget"
STOP_DEADLINE = "deadline"


@dataclass(frozen=True)
class SubagentSettings:
    """The S13 bounds, resolved from a UserConfig (or defaults)."""

    enabled: bool = True
    max_per_turn: int = 4
    timeout_s: int = 120
    token_budget: int = 20000
    cost_budget_usd: float = 0.0


def _int_or(value: Any, default: int) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value: Any, default: float) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def subagent_settings(user_config: Any | None) -> SubagentSettings:
    """Resolve the spawn bounds from a config object (None = defaults).

    Attribute-tolerant like the spawn policy's own reads: a missing
    or partial config yields the documented defaults rather than an
    exception on the routing path.
    """
    return SubagentSettings(
        enabled=bool(getattr(user_config, "subagents_enabled", True)),
        max_per_turn=max(0, _int_or(
            getattr(user_config, "subagents_max_per_turn", 4), 4)),
        timeout_s=max(1, _int_or(
            getattr(user_config, "subagent_timeout_s", 120), 120)),
        token_budget=max(0, _int_or(
            getattr(user_config, "subagent_token_budget", 20000), 20000)),
        # P1e: the cost budget is the schema's per-delegation cost
        # budget (its semantics extend S13's to all delegations),
        # read through the delegation-settings resolver — the
        # single read path. The resolver composes it from
        # subagent_cost_budget_usd, its storage field, so the value
        # is unchanged. S13's own per-sub-agent bounds above
        # (enabled / max_per_turn / timeout / token budget) are
        # not delegation settings and stay S13's.
        cost_budget_usd=max(0.0, _float_or(
            resolve_delegation_settings(user_config)
            .per_delegation_cost_budget_usd, 0.0)),
    )


def spawn_tool_definition() -> dict[str, Any]:
    """The spawn tool offered to specialists (OpenAI function shape).

    Offered by ``run_specialist`` only — it is not a registry tool,
    so it never appears in the registry, in per-agent allow-lists,
    or in a child run's surface. Execution is intercepted in
    ``execute_tool_node`` and lands in :func:`execute_spawn`.
    """
    return {
        "type": "function",
        "function": {
            "name": SPAWN_TOOL_NAME,
            "description": (
                "Spawn a bounded sub-agent — one of the existing "
                "specialist roles — for a focused sub-task, and "
                "receive its result as a summary. The sub-agent works "
                "on its own and cannot spawn further sub-agents. It "
                "gets read-only tools by default; list additional "
                "tool names in 'tools' only when the sub-task truly "
                "needs them (you can only grant tools you have "
                "yourself, and every call it makes passes the same "
                "policy and approval checks as yours). Spawns are "
                "capped per turn and each sub-agent has time and "
                "token budgets; a spawn over any bound is denied with "
                "the reason. Use it for a well-scoped piece of work "
                "you can describe in one prompt; do the work yourself "
                "when it is small or tightly coupled to what you are "
                "already doing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": list(SPECIALIST_ROLES),
                        "description": "Which specialist role should run the sub-task.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "The focused sub-task, stated completely: "
                            "the sub-agent sees this prompt and the "
                            "team blackboard, not your conversation."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional: extra tool names beyond the "
                            "read-only default. Only tools available "
                            "to you in this run can be granted."
                        ),
                    },
                },
                "required": ["role", "task"],
            },
        },
    }


# ── Usage accounting (the budget enforcement read) ──────────────────

def _usage_marker(parent_agent: str, role: str, seq: int) -> str:
    """The UsageLedger task_class marking one spawn's provider calls."""
    return f"subagent:{parent_agent}:{role}:{seq}"


def subagent_usage(task_id: str | None, marker: str | None) -> tuple[int, float]:
    """(tokens, cost USD) the ledger recorded for one spawn.

    Reads ``UsageLedger`` rows for the parent task filtered to the
    spawn's task-class marker. Never raises: a ledger read failure
    reports (0, 0.0) and the timeout / per-turn cap still bind — the
    failure is logged, not hidden.
    """
    if not task_id or not marker:
        return 0, 0.0
    try:
        data = get_usage_ledger().query(task_id=task_id)
        tokens = 0
        cost = 0.0
        for entry in data.get("entries") or []:
            if entry.get("task_class") == marker:
                tokens += int(entry.get("total_tokens") or 0)
                cost += float(entry.get("cost_usd") or 0.0)
        return tokens, cost
    except Exception:
        logger.debug("Sub-agent usage read failed", exc_info=True)
        return 0, 0.0


async def _budget_stop(
    task_id: str | None, marker: str | None, settings: SubagentSettings,
) -> tuple[str, int, float] | None:
    """(stop_kind, tokens, cost) when a budget is already exhausted.

    Checked before every child model call (the child node) and
    before a child approval is surfaced to the user (the segment
    loop): a child over budget stops, it does not pause for an
    approval the user would be granting to work that cannot finish.
    """
    if not task_id or not marker:
        return None
    if settings.token_budget <= 0 and settings.cost_budget_usd <= 0:
        return None
    tokens, cost = await asyncio.to_thread(subagent_usage, task_id, marker)
    if settings.token_budget > 0 and tokens >= settings.token_budget:
        return STOP_TOKEN_BUDGET, tokens, cost
    if settings.cost_budget_usd > 0 and cost >= settings.cost_budget_usd:
        return STOP_COST_BUDGET, tokens, cost
    return None


# ── Tool surface ────────────────────────────────────────────────────

def resolve_child_surface(
    registry: Any | None,
    configurable: dict[str, Any],
    parent_agent: str,
    requested: Any,
) -> tuple[list[str], str | None]:
    """The child's tool names, or (empty surface, denial reason).

    Default surface: the registry's read-only tools the PARENT
    specialist itself has this run (its per-agent allow-list when
    one is set, else every registry tool). A spawn may request more
    by name — granted only from the parent's own surface; anything
    else is a denial with the reason, never a silent clamp (S4-style
    surfacing). The spawn tool itself is never grantable. (Plan mode
    never reaches here: spawns are denied in plan mode, at the seam.)
    """
    if registry is None:
        parent_surface: set[str] = set()
        read_only: set[str] = set()
    else:
        all_names = {tool.name for tool in registry.get_tools()}
        allowed = (configurable.get("agent_tools") or {}).get(parent_agent)
        parent_surface = (
            set(allowed) & all_names if isinstance(allowed, list)
            else set(all_names))
        read_only = registry.read_only_names()
    surface = set(read_only) & parent_surface
    if requested is not None:
        if (not isinstance(requested, list)
                or not all(isinstance(name, str) for name in requested)):
            return [], "the spawn request's 'tools' must be a list of tool names"
        for name in requested:
            if name == SPAWN_TOOL_NAME:
                # Never grantable — depth is enforced by construction,
                # not by negotiation with the requesting model.
                continue
            if name not in parent_surface:
                return [], (
                    f"tool '{name}' is not available to the "
                    f"{parent_agent} specialist in this run, so a "
                    f"sub-agent cannot be given it")
            surface.add(name)
    return sorted(surface), None


# ── Activity (S12-shaped NODE_ACTIVITY on the task bus) ─────────────

def _publish_activity(
    state: dict[str, Any],
    *,
    action: str,
    status: str,
    parent_agent: str,
    role: str,
    seq: int,
    task: str,
    reason: str = "",
    output: dict[str, Any] | None = None,
) -> None:
    """Publish one sub-agent activity event for the parent task.

    Same payload shape as S12's delegation events (task_id /
    from_node / to_node / action / reason / briefing / input_payload
    / output_payload / status / timestamp) plus the parent linkage
    (``parent_agent``, ``subagent_seq``). The executor's bus pump
    funnels bus events into the task stream and the S3 ledger tap on
    both paths. Observational: a publish failure never breaks a
    spawn.
    """
    task_id = state.get("task_id")
    if not task_id:
        return
    excerpt = " ".join((task or "").split())[:TASK_EXCERPT_CHARS]
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": state.get("session_id"),
        "from_node": parent_agent,
        "to_node": role,
        "action": action,
        "reason": reason or excerpt,
        "briefing": excerpt,
        "input_payload": {
            "subagent_task": task,
            "parent_agent": parent_agent,
            "parent_task_id": task_id,
        },
        "output_payload": output or {},
        "status": status,
        "timestamp": int(time.time()),
        "subagent_seq": seq,
        "parent_agent": parent_agent,
    }
    try:
        event_bus.publish(task_id, Event(
            type=EventType.NODE_ACTIVITY,
            request_id=str(task_id),
            payload=payload,
        ))
    except Exception:
        logger.debug("Sub-agent activity publish failed", exc_info=True)


# ── The child specialist node ───────────────────────────────────────

def _spec_for(role: str | None):
    from aether_engine.langgraph.nodes.coder import CODER_SPEC
    from aether_engine.langgraph.nodes.planner import PLANNER_SPEC
    from aether_engine.langgraph.nodes.researcher import RESEARCHER_SPEC

    return {"researcher": RESEARCHER_SPEC, "planner": PLANNER_SPEC,
            "coder": CODER_SPEC}[role]


def _null_artifact_saver(*args: Any, **kwargs: Any) -> str:
    """Children write no artifacts: their product is the summary.

    ``run_specialist`` saves the role's artifact on a plain-text
    finish; a child writing ``researcher_output.txt`` would collide
    with the parent specialist's own artifact for the same task, so
    the child run passes this no-op saver instead.
    """
    return ""


def _stop_text(kind: str, settings: SubagentSettings, tokens: int, cost: float) -> str:
    if kind == STOP_TOKEN_BUDGET:
        return (
            f"Sub-agent stopped: the per-sub-agent token budget "
            f"({settings.token_budget:,} tokens) is exhausted — "
            f"{tokens:,} tokens are recorded in the usage ledger. "
            f"Reporting back without further model calls.")
    if kind == STOP_COST_BUDGET:
        return (
            f"Sub-agent stopped: the per-sub-agent cost budget "
            f"(${settings.cost_budget_usd:.2f}) is exhausted — "
            f"${cost:.4f} is recorded in the usage ledger. "
            f"Reporting back without further model calls.")
    return (
        f"Sub-agent stopped: the per-sub-agent time budget "
        f"({settings.timeout_s}s) is exhausted. Reporting back "
        f"without further model calls.")


async def _child_node(state: dict[str, Any], config: RunnableConfig) -> dict:
    """The child graph's specialist node: budget gate + run_specialist.

    The gate runs before EVERY model call of the child (each
    tool-loop round re-enters this node), so a budget stop lands
    between calls, never mid-call — overshoot is at most the call
    already in flight. A stop returns a plain assistant message with
    no tool call, which routes the child graph to its END with the
    stop recorded in ``subagent_stop`` for the spawn seam.
    """
    from aether_engine.langgraph.nodes.runner import run_specialist

    configurable = config.get("configurable", {}) or {}
    spec = _spec_for(state.get("active_specialist"))
    settings = subagent_settings(configurable.get("user_config"))
    stop = await _budget_stop(
        state.get("task_id"), configurable.get("subagent_marker"), settings)
    kind: str | None = None
    tokens = 0
    cost = 0.0
    if stop is not None:
        kind, tokens, cost = stop
    else:
        deadline = state.get("subagent_deadline")
        if deadline is not None and time.time() >= deadline:
            kind = STOP_DEADLINE
            tokens, cost = await asyncio.to_thread(
                subagent_usage, state.get("task_id"),
                configurable.get("subagent_marker"))
    if kind is not None:
        return {
            "messages": [{
                "role": "assistant",
                "content": _stop_text(kind, settings, tokens, cost),
            }],
            "current_phase": spec.phase,
            "subagent_stop": kind,
        }
    # Attribute this call's usage to the spawn: the provider layer
    # records under the ambient usage context, and the marker is
    # what _budget_stop reads back. Set here, inside the node, so
    # the attribution holds regardless of how graph runtimes copy
    # context across invoke boundaries (the segment loop sets the
    # same context around the whole child invoke as well).
    with usage_context(
            session_id=state.get("session_id"),
            task_id=state.get("task_id"),
            task_class=configurable.get("subagent_marker")):
        return await run_specialist(
            spec, state, config, save_artifact=_null_artifact_saver)


# ── Child run construction ──────────────────────────────────────────

def _derived_user_config(user_config: Any | None) -> Any | None:
    """The child's view of the user config.

    Policies, path rules, approval mode, per-agent models and tool
    policies carry over UNCHANGED (the child's pipeline is the
    parent's pipeline). Three things are deliberately not inherited:
    prewalk (a child runs its role's own model from the first call —
    prewalk is a parent-run planning optimisation), per-agent output
    schemas (a sub-task's answer is a summary for the parent, not
    the role's deliverable), and the advisor (a child does not spend
    extra model calls reviewing itself).
    """
    if user_config is None:
        return None
    try:
        import dataclasses

        aux = {
            key: value
            for key, value in (
                getattr(user_config, "auxiliary_models", None) or {}).items()
            if key != "advisor"
        }
        return dataclasses.replace(
            user_config,
            prewalk_agents=[],
            agent_output_schemas={},
            auxiliary_models=aux,
        )
    except Exception:  # noqa: BLE001 — a non-dataclass config passes through whole.
        return user_config


def _child_initial_state(
    state: dict[str, Any],
    *,
    role: str,
    task: str,
    parent_agent: str,
    deadline: float,
) -> dict[str, Any]:
    """Seed the child graph's state (the executor's seed, narrowed).

    The child shares the parent's task/session/message identity —
    its tool executions, events and usage rows belong to the parent
    task — but starts with its own conversation: the sub-task prompt
    plus the team blackboard briefing, never the parent's transcript.
    """
    from aether_engine.langgraph.state import blackboard_briefing

    content = (
        f"You are a bounded sub-agent spawned by the {parent_agent} "
        f"specialist for one focused sub-task. Complete only this "
        f"sub-task and report the result concisely — your final "
        f"message is the result the {parent_agent} specialist will "
        f"read.\n\nSub-task: {task}")
    board = blackboard_briefing(state)
    if board:
        content += f"\n\nTeam blackboard so far:\n{board}"
    return {
        "original_prompt": task,
        "messages": [{"role": "user", "content": content}],
        "plan": "No plan currently.",
        "current_phase": "initializing",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": role,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "write_tool_used": False,
        "delegation_log": [],
        "delegation_count": 0,
        "subagent_count": 0,
        "subagent_stop": None,
        "subagent_deadline": deadline,
        "blackboard": {},
        "structured_outputs": {},
        "plan_mode": bool(state.get("plan_mode")),
        "context_summary": None,
        "context_summary_upto": 0,
        "task_id": state.get("task_id"),
        "session_id": state.get("session_id"),
        "message_id": state.get("message_id"),
        "context_briefing": state.get("context_briefing"),
        "active_skills": [],
        "run_config": None,
    }


def _attempt_status(values: dict[str, Any] | None) -> str:
    stop = (values or {}).get("subagent_stop")
    if stop in (STOP_TOKEN_BUDGET, STOP_COST_BUDGET):
        return "budget_exhausted"
    if stop == STOP_DEADLINE:
        return "timed_out"
    return "completed"


async def _run_child_attempt(
    state: dict[str, Any],
    configurable: dict[str, Any],
    *,
    role: str,
    task: str,
    surface: list[str],
    settings: SubagentSettings,
    marker: str,
    seq: int,
    deadline: float,
    parent_agent: str,
    attempt: int,
    escalate: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Run (or resume) the child graph until it finishes or pauses.

    The bridging loop: inspect the child's checkpoint; when it sits
    at an approval pause, obtain the decision through THIS node's
    own ``interrupt()`` — the parent graph pauses with the child's
    payload — and forward the decision into the child. On a replay
    of the parent node the same loop re-derives everything from the
    child's durable checkpoint, so the sequence of ``interrupt()``
    calls is identical on every pass. Approval wait time is not
    charged against the deadline: each active segment is bounded by
    the time REMAINING when the segment starts.
    """
    from aether_engine.langgraph.graph import _build_specialist_graph

    parent_thread = (
        configurable.get("thread_id") or state.get("session_id") or "task")
    child_thread = f"{parent_thread}:sub:{state.get('task_id')}:{seq}"
    if attempt > 1:
        child_thread += f":try{attempt}"
    child_configurable = dict(configurable)
    child_configurable.update({
        "thread_id": child_thread,
        "agent_tools": {role: list(surface)},
        "user_config": _derived_user_config(configurable.get("user_config")),
        "is_subagent_run": True,
        "subagent_marker": marker,
    })
    if escalate:
        # S14: the retry after a genuine child failure runs one
        # tier up — the flag is all this seam passes; the child's
        # own model resolution (run_specialist, auto mode only,
        # never for a pinned role) applies the step-up from its
        # fresh decision and records it as an escalation.
        child_configurable["model_routing_escalate"] = True
    child_config = {
        "configurable": child_configurable,
        "recursion_limit": CHILD_RECURSION_LIMIT,
    }
    child_state = _child_initial_state(
        state, role=role, task=task, parent_agent=parent_agent,
        deadline=deadline)
    SUBAGENT_CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)
    started = False
    last_values: dict[str, Any] | None = None
    # A child pause surfaces in one of two shapes depending on the
    # ambient graph context: the invoke result's "__interrupt__" key,
    # or a GraphInterrupt raised out of the invoke (LangGraph's
    # native subgraph-propagation shape, seen when the child runs
    # inside a parent node). Both are the same event; the raised
    # shape carries the pending interrupts, kept here for the next
    # pass when the checkpoint read has not caught up.
    pending_override: list | None = None
    async with AsyncSqliteSaver.from_conn_string(
            str(SUBAGENT_CHECKPOINT_DB)) as saver:
        await saver.setup()
        child = _build_specialist_graph(role, _child_node, checkpointer=saver)
        while True:
            if pending_override is not None:
                pending = pending_override
                pending_override = None
                cstate_values: dict[str, Any] = dict(last_values or {})
            else:
                cstate = await child.aget_state(child_config)
                pending = list(cstate.interrupts) if cstate is not None else []
                cstate_values = dict(cstate.values or {}) if cstate is not None else {}
            if pending:
                # Budget gate BEFORE the approval is surfaced (see
                # _budget_stop): the user never approves work that a
                # budget has already stopped.
                stop = await _budget_stop(
                    state.get("task_id"), marker, settings)
                if stop is not None:
                    kind = stop[0]
                    return (
                        "timed_out" if kind == STOP_DEADLINE
                        else "budget_exhausted",
                        cstate_values)
                decision = interrupt(pending[0].value)
                segment: Any = Command(resume=decision)
            elif started:
                return _attempt_status(last_values), last_values
            else:
                segment = child_state
                started = True
            remaining = deadline - time.time()
            if remaining <= 0:
                return "timed_out", last_values
            try:
                with usage_context(
                        session_id=state.get("session_id"),
                        task_id=state.get("task_id"),
                        task_class=marker):
                    out = await asyncio.wait_for(
                        child.ainvoke(segment, child_config), timeout=remaining)
            except GraphInterrupt as gi:
                carried = gi.args[0] if gi.args else None
                pending_override = (
                    list(carried) if isinstance(carried, (list, tuple)) else [])
                continue
            last_values = out
            if not (isinstance(out, dict) and out.get("__interrupt__")):
                return _attempt_status(out), out


# ── Summary (the I/O contract back to the parent) ───────────────────

def _child_result(values: dict[str, Any] | None) -> tuple[str, list[str]]:
    """(final answer text, tool names used) from a child run's state."""
    from aether_engine.langgraph.state import message_dicts

    result = ""
    tools_used: list[str] = []
    for msg in message_dicts((values or {}).get("messages") or []):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            name = name or tc.get("name")
            if name and name not in tools_used:
                tools_used.append(str(name))
        if msg.get("content"):
            result = str(msg["content"])
    return result, tools_used


def _summary_text(
    *,
    status: str,
    role: str,
    seq: int,
    task: str,
    result: str,
    tools_used: list[str],
    tokens: int,
    duration_s: float,
    attempts: int,
    detail: str,
    settings: SubagentSettings,
) -> str:
    """The bounded tool result the parent specialist reads."""
    head = (
        f"Sub-agent result — {role} (spawn #{seq} this turn), "
        f"{status.replace('_', ' ')} in {duration_s:.1f}s, "
        f"{tokens:,} tokens recorded"
        + (f", attempt {attempts}" if attempts > 1 else "") + ".")
    lines = [head, f"Sub-task: {' '.join(task.split())[:TASK_EXCERPT_CHARS]}"]
    if status == "completed":
        if tools_used:
            lines.append(f"Tools used: {', '.join(tools_used)}")
        lines.append("Result:")
        lines.append(result.strip()[:RESULT_EXCERPT_CHARS] or "(no text result)")
    elif status == "failed":
        lines.append(f"Failure: {detail[:DETAIL_MAX_CHARS]}")
        lines.append(
            "No result was produced. Continue without it, take a "
            "different approach, or spawn again if the failure looks "
            "transient.")
    elif status == "timed_out":
        lines.append(
            f"The sub-agent did not finish within the "
            f"{settings.timeout_s}s time budget and was stopped"
            + (f": {detail[:DETAIL_MAX_CHARS]}" if detail else "."))
        lines.append(
            "Treat the sub-task as not done; narrow it, do it "
            "yourself, or spawn again with a smaller scope.")
    else:  # budget_exhausted
        lines.append(result.strip()[:RESULT_EXCERPT_CHARS] or detail[:DETAIL_MAX_CHARS])
        lines.append(
            "Treat the sub-task as not done; narrow it or do the "
            "remaining part yourself.")
    return "\n".join(lines)[:SUMMARY_MAX_CHARS]


def _blackboard_line(
    *,
    status: str,
    role: str,
    seq: int,
    result: str,
    detail: str,
    settings: SubagentSettings,
) -> str:
    """The one-line digest the team (and the supervisor) sees."""
    if status == "completed":
        digest = " ".join((result or "").split())[:240] or "completed with no text result"
        return f"[Sub-agent {role} #{seq}] {digest}"
    if status == "failed":
        return f"[Sub-agent {role} #{seq}] FAILED: {' '.join(detail.split())[:160]}"
    if status == "timed_out":
        return f"[Sub-agent {role} #{seq}] TIMED OUT after {settings.timeout_s}s — no result"
    return f"[Sub-agent {role} #{seq}] STOPPED — budget exhausted, no result"


# ── The spawn seam (called from execute_tool_node) ──────────────────

async def execute_spawn(
    state: dict[str, Any],
    config: dict[str, Any],
    pending: dict[str, Any],
) -> dict[str, Any]:
    """Execute one ``spawn_subagent`` pending call; return the update.

    The S4-shaped contract: a denial is a tool result with the
    reason (never a silent no-op), is logged, and is published as
    structured activity; a started spawn ALWAYS returns a bounded
    summary, whatever happened inside the child. The per-turn count
    increments only for started spawns, in this returned update —
    so an approval pause mid-spawn counts it exactly once, when the
    spawn completes and the update commits.
    """
    configurable = config.get("configurable", {}) or {}
    registry = configurable.get("tool_registry")
    user_config = configurable.get("user_config")
    settings = subagent_settings(user_config)
    parent_agent = state.get("active_specialist") or "specialist"
    args = pending.get("args") or {}
    call_id = pending.get("call_id") or pending.get("id") or f"call_{SPAWN_TOOL_NAME}"
    role = str(args.get("role") or "").strip().lower()
    task = str(args.get("task") or "").strip()
    count = state.get("subagent_count") or 0
    seq = count + 1

    def _denied(reason: str) -> dict[str, Any]:
        logger.warning(
            "Sub-agent spawn by %s denied (would-be spawn #%d): %s",
            parent_agent, seq, reason)
        _publish_activity(
            state, action="subagent_denied", status="denied",
            parent_agent=parent_agent, role=role or "?", seq=seq,
            task=task, reason=reason,
            output={"status": "denied", "reason": reason})
        return {
            "messages": [{
                "role": "tool",
                "name": SPAWN_TOOL_NAME,
                "content": f"Error: Sub-agent spawn denied: {reason}",
                "tool_call_id": call_id,
            }],
            "pending_tool_call": None,
        }

    # The spawn call's OWN policy verdict, resolved with the same
    # inputs the graph's tool_router used: an explicit per-tool
    # "Deny" on spawn_subagent (or a matching path rule) sends the
    # call here rather than to the pause node, and the seam honours
    # it with the pipeline's own denial wording. ("Require Approval"
    # pauses the spawn itself upstream and arrives here approved;
    # the child's calls remain individually gated either way.)
    from aether_engine.tools.policy import resolve_policy_by_name

    verdict = resolve_policy_by_name(
        SPAWN_TOOL_NAME,
        user_config.tool_policies if user_config else {},
        registry,
        args=args,
        path_rules=user_config.path_rules if user_config else None,
        shell_extras=(
            {"deny": user_config.extra_shell_deny_patterns,
             "ask": user_config.extra_shell_ask_patterns}
            if user_config else None),
        approval_mode=user_config.approval_mode if user_config else "ask",
    )
    if verdict == "deny":
        reason = "blocked by user policy (Deny)"
        logger.warning(
            "Sub-agent spawn by %s denied by policy (would-be spawn #%d)",
            parent_agent, seq)
        _publish_activity(
            state, action="subagent_denied", status="denied",
            parent_agent=parent_agent, role=role or "?", seq=seq,
            task=task, reason=reason,
            output={"status": "denied", "reason": reason})
        return {
            "messages": [{
                "role": "tool",
                "name": SPAWN_TOOL_NAME,
                "content": f"Error: Tool '{SPAWN_TOOL_NAME}' is blocked by user policy (Deny).",
                "tool_call_id": call_id,
            }],
            "pending_tool_call": None,
        }

    if not settings.enabled:
        return _denied(
            "sub-agents are disabled — enable them in Settings → "
            "Agent Orchestration → Sub-agents")
    if state.get("plan_mode"):
        # Mirrors the supervisor spawn policy's plan-mode stance
        # (E1-11): plan mode is for planning, and a spawn is an
        # execution act — the tool is not even offered there; this
        # backstop covers a forged or stale pending call.
        return _denied(
            "plan mode is active: sub-agent spawns are disabled in "
            "plan mode")
    if role not in SPECIALIST_ROLES:
        return _denied(
            f"unknown sub-agent role {role!r} — the role must be one "
            f"of: {', '.join(SPECIALIST_ROLES)}")
    if not task:
        return _denied("the spawn request named no sub-task (the 'task' argument is empty)")
    if count >= settings.max_per_turn:
        return _denied(
            f"the sub-agent cap for one turn "
            f"({settings.max_per_turn}) is reached")
    surface, denial = resolve_child_surface(
        registry, configurable, parent_agent, args.get("tools"))
    if denial is not None:
        return _denied(denial)

    marker = _usage_marker(parent_agent, role, seq)
    started_at = time.time()
    deadline = started_at + settings.timeout_s
    _publish_activity(
        state, action="subagent_spawn", status="running",
        parent_agent=parent_agent, role=role, seq=seq, task=task,
        output={"status": "running", "tools": surface})

    status = "failed"
    detail = ""
    child_values: dict[str, Any] | None = None
    attempts = 0
    retry_escalate = False
    while attempts < 2:
        attempts += 1
        try:
            status, child_values = await _run_child_attempt(
                state, configurable,
                role=role, task=task, surface=surface, settings=settings,
                marker=marker, seq=seq, deadline=deadline,
                parent_agent=parent_agent, attempt=attempts,
                escalate=retry_escalate)
            detail = ""
            break
        except GraphInterrupt:
            # Approval bridging: this seam's own interrupt() (or a
            # pause propagating out of the child) is graph control
            # flow, not a child failure — it is never retried and
            # never summarised. The parent graph pauses; on resume
            # this whole seam replays and the bridging loop forwards
            # the decision into the child's checkpoint.
            raise
        except TimeoutError:
            # asyncio.wait_for expiry (TimeoutError on 3.11+): the
            # wall-clock budget is gone; timeouts never retry.
            status = "timed_out"
            detail = f"exceeded the {settings.timeout_s}s wall-clock budget"
            child_values = None
            break
        except Exception as exc:  # noqa: BLE001 — child-run boundary: one retry, then an honest failure summary.
            detail = f"{type(exc).__name__}: {exc}"[:DETAIL_MAX_CHARS]
            child_values = None
            if attempts >= 2 or time.time() >= deadline - 1:
                status = "failed"
                break
            # S14: the retry is the child's escalation point — a
            # genuine failure (never a denial, timeout or budget
            # stop: those return a status above, they do not raise)
            # retries one model tier up under auto routing.
            retry_escalate = True
            logger.warning(
                "Sub-agent %s #%d (parent %s) failed on attempt %d "
                "(%s); retrying once within the time budget",
                role, seq, parent_agent, attempts, detail)

    duration_s = time.time() - started_at
    tokens, cost = subagent_usage(state.get("task_id"), marker)
    result, tools_used = _child_result(child_values)
    if status == "budget_exhausted" and not detail:
        detail = result[:DETAIL_MAX_CHARS]
    summary = _summary_text(
        status=status, role=role, seq=seq, task=task, result=result,
        tools_used=tools_used, tokens=tokens, duration_s=duration_s,
        attempts=attempts, detail=detail, settings=settings)
    _publish_activity(
        state,
        action="subagent_completed" if status == "completed" else "subagent_failed",
        status=status,
        parent_agent=parent_agent, role=role, seq=seq, task=task,
        output={
            "status": status,
            "summary": summary,
            "tokens_used": tokens,
            "cost_usd": cost,
            "duration_ms": int(duration_s * 1000),
            "attempts": attempts,
            "tools_used": tools_used,
        })
    logger.info(
        "Sub-agent %s #%d (parent %s) %s in %.1fs, %d tokens, %d attempt(s)",
        role, seq, parent_agent, status, duration_s, tokens, attempts)
    return {
        "messages": [{
            "role": "tool",
            "name": SPAWN_TOOL_NAME,
            "content": summary,
            "tool_call_id": call_id,
        }],
        "pending_tool_call": None,
        "subagent_count": seq,
        "blackboard": {"findings": [_blackboard_line(
            status=status, role=role, seq=seq, result=result,
            detail=detail, settings=settings)]},
    }
