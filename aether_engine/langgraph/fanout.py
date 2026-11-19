"""Parallel fan-out for independent delegations (P2b, K-12).

The supervisor's delegations were strictly sequential: a task's
latency was the sum of every specialist call, even when two
delegations had nothing to do with each other. P2b lets the
supervisor declare a **parallel group** — a pair of independent
delegations emitted together — and runs the members concurrently.

**Principle: fan out children, not the shared graph.** The shared
``AetherState`` is never multi-threaded (concurrent specialists
writing the same channels is a race-condition farm, and approval
pauses from two branches would interleave into nonsense). Instead
each member runs as an isolated child execution on the S13 pattern
(``langgraph/subagents.py``): fresh checkpoint thread, derived
state and user config, its own provider resolution through S14
inside ``run_specialist``, its own budgets, and the same policy ->
approval -> execution pipeline as any specialist work. Results
join in **declaration order**, never completion order, so the
supervisor's next decision and the delegation log see a
deterministic history regardless of which child won the race.

**Checkpoint files: one per member.** Child checkpoints never
touch the main graph's ``checkpoints.db`` (RA-17). They also
cannot share S13's single ``subagent_checkpoints.db`` file:
concurrent Pregel loops writing checkpoints into one SQLite file
corrupt each other's interrupt state (proven during this slice's
build — with a shared file, a member's approval pause surfaced
unreadably or vanished outright, race-dependently; with separate
files both members pause and resume exactly). Each member
therefore checkpoints into a per-member sibling file
(``subagent_checkpoints.fanout-<group>-<index>.db`` beside the
sub-agent database), through ONE saver connection per file kept
open across the group's coordinator passes: a fresh connection's
snapshot of a just-paused child thread is unreliable with the
SQLite saver (intermittently empty although the rows are durable
— also proven during the build), while the writing connection
reads its own checkpoints exactly, which is how S13's bridging
reads them too. The group record (below) lives in
``subagent_checkpoints.db`` itself — the coordinator is its only
writer and it runs no graph loop.

**Declaration.** Groups exist only on the P1d subagents-as-tools
topology: the supervisor calls the ``delegate_in_parallel`` tool
with ``members = [{"specialist": ..., "task": ...}, ...]``. A
member sees only its own task text plus the team blackboard — not
the conversation — exactly like an S13 sub-agent. The legacy
topology binds no group tool, so no group can be declared there
and sequential behaviour is untouched. The post-model hook counts
a group of N as N delegations against ``supervisor_delegation_cap``
(no free delegations) and writes one delegation-log entry per
member; policy (plan mode, spawn allow-list, the cap) is judged
there, before anything runs.

**Gates (Gate 4 defaults; owner ratification pending).** Parallel
scheduling requires ALL of: the resolved delegation mode is
``auto-broadcast`` (P1e — this slice flips its availability entry),
the resolved ``max_parallel_delegations`` width covers the group,
the member multiset is an allowed pair — {researcher, planner} or
{researcher, researcher}; **coder is excluded in v1** (two coders
writing one worktree is a merge problem, not a routing problem) —
and fewer than 2 groups already ran this turn. Anything else
degrades to **sequential** execution of the same isolated children
in declaration order, with the reason logged and carried on the
group's activity event: a group declaration is never an error.
With default settings (mode ``auto-coordinate``, width 1) fan-out
is off and every group runs sequentially — the plan's rollback
posture.

**Approvals: one pause at a time, globally.** Members compute
concurrently, but approval pauses are surfaced one at a time, in
declaration order (FIFO): the coordinator bridges a paused member
through its own ``interrupt()`` in the supervisor node — the S13
bridging discipline, so the sequence of ``interrupt()`` calls is
identical on every replay and LangGraph hands each resume decision
to the right member. A member's pause is durably held in its own
child checkpoint while it waits its turn, so no pause is ever
doubled or lost. A member already over budget is never surfaced
for approval (the S13 budget gate). The ADR-0030 approval cache is
shared across siblings by construction: children run with the
parent's own policy inputs and task identity, so a grant one child
earns covers an identical sibling request. One member's denial,
timeout or budget stop never cancels a sibling; the join reports
each outcome independently.

**Resume (D-1).** A group record — one row per group in a
``parallel_groups`` table in the sub-agent checkpoint database —
tracks per-member status as the group runs. Re-entering a group
(the tool replays after a pause, or a task resume re-emits the
same declaration) classifies the record: all members DONE ->
replay the recorded outcomes, re-run nothing; all NOT DONE ->
run the group; a mix -> run only the unfinished members; **any
member UNCERTAIN** (it was mid-run when execution stopped, so its
tools may or may not have executed) -> **the group stops and
asks**: finished members report their recorded outcomes, the
uncertain member is named and is never silently re-run.

**Honest limits.** Members are flagged as sub-agent runs, so —
like S13 children — they cannot themselves spawn sub-agents in v1
(a sequential researcher delegation still can). A failed member is
not retried inside the group (S13's single retry is a spawn-seam
behaviour); its failure is reported and the supervisor may
re-delegate. Approval wait time is not charged against a member's
budget: deadlines are computed per coordinator pass, as in S13.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command, interrupt

from aether_common.contracts import Event, EventType
from aether_engine.delegation_settings import resolve_delegation_settings
from aether_engine.event_bus import event_bus
from aether_engine.langgraph import subagents

logger = logging.getLogger("aether_engine.langgraph.fanout")

#: The supervisor tool that declares a parallel group (tools
#: topology only — see the module docstring).
GROUP_TOOL_NAME = "delegate_in_parallel"

#: Gate-4 recommended caps (owner ratification pending): groups are
#: at most this wide, and at most this many groups run per turn.
#: The width is further bounded by the owner's resolved
#: ``max_parallel_delegations`` (P1e) — both caps apply.
PLAN_MAX_WIDTH = 2
PLAN_MAX_GROUPS_PER_TURN = 2

#: The Gate-4 pair allowlist for v1, as sorted member multisets.
#: Coder is excluded (see the module docstring).
ALLOWED_GROUP_PAIRS: tuple[tuple[str, ...], ...] = (
    ("planner", "researcher"),
    ("researcher", "researcher"),
)

#: Member statuses that are final: the member's fate is recorded
#: and it is never re-run (the D-1 DONE classification).
FINAL_MEMBER_STATUSES = frozenset({
    "completed", "failed", "timed_out", "budget_exhausted", "denied",
})

#: Bounds on what a group reports back to the supervisor.
MEMBER_SUMMARY_MAX_CHARS = 1200
GROUP_TEXT_MAX_CHARS = 6000
TASK_EXCERPT_CHARS = 200

#: The child graph's own step ceiling (S13's, shared value).
_MEMBER_RECURSION_LIMIT = subagents.CHILD_RECURSION_LIMIT


# ── Members: parsing and structural validation ────────────────────

@dataclass(frozen=True)
class FanoutMember:
    """One declared group member: a specialist + its complete task."""

    specialist: str
    task: str
    tier: str | None = None


def parse_group_members(raw: Any) -> tuple[list[FanoutMember], str | None]:
    """Parse a group tool call's ``members`` argument.

    Returns (members, None) or ([], reason). Structural only —
    pair/width/policy judgements live in validate_group and the
    post-model hook. A member's ``tier`` (P1a's emission, when it
    lands) is carried through untouched; nothing here consumes it
    yet, and its absence changes nothing.
    """
    if not isinstance(raw, list) or not raw:
        return [], "the group declaration's 'members' must be a non-empty list"
    members: list[FanoutMember] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"member {i + 1} is not an object"
        specialist = str(item.get("specialist") or "").strip().lower()
        task = str(item.get("task") or "").strip()
        if specialist not in subagents.SPECIALIST_ROLES:
            return [], (
                f"member {i + 1} names unknown specialist "
                f"{specialist!r} — the specialist must be one of: "
                f"{', '.join(subagents.SPECIALIST_ROLES)}")
        if not task:
            return [], f"member {i + 1} ({specialist}) names no task"
        tier = item.get("tier")
        members.append(FanoutMember(
            specialist=specialist, task=task,
            tier=str(tier) if isinstance(tier, str) and tier.strip() else None))
    return members, None


def validate_group(members: list[FanoutMember]) -> str | None:
    """Why this group may not run in PARALLEL, or None when it may.

    Structural fan-out validity only (the Gate-4 allowlist); the
    caller degrades to sequential execution on any reason — an
    invalid group is never an error. Policy (plan mode, the spawn
    allow-list, the delegation cap) is NOT judged here: it is the
    post-model hook's, and a policy-denied specialist never runs in
    any scheduling.
    """
    if len(members) < 2:
        return "a parallel group needs at least two members"
    names = sorted(m.specialist for m in members)
    if "coder" in names:
        return (
            "coder is excluded from parallel groups in v1 (two "
            "coders writing one worktree is a merge problem, not a "
            "routing problem)")
    if tuple(names) not in ALLOWED_GROUP_PAIRS:
        return (
            "parallel groups are pairs of researcher/planner in v1 "
            f"(allowed: researcher + planner, researcher + "
            f"researcher); got {' + '.join(names)}")
    return None


def group_id_for(task_id: str | None, members: list[FanoutMember]) -> str:
    """The deterministic id of one declared group.

    Derived from the task and the declaration itself, so the
    post-model hook, the group tool and any later re-entry of the
    same declaration (a pause replay, a task resume that re-emits
    it) all compute the same id and find the same group record.
    """
    signature = json.dumps(
        [[m.specialist, m.task, m.tier] for m in members],
        ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{task_id or 'task'}:fanout:{digest}"


def groups_already_declared(state: dict[str, Any]) -> set[str]:
    """Group ids already declared this turn (from the delegation log).

    The hook writes one log entry per member carrying the group id,
    so the log is the turn's record of groups — the same source the
    S4 counter falls back to.
    """
    ids: set[str] = set()
    for entry in (state or {}).get("delegation_log") or []:
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision") or {}
        if isinstance(decision, dict) and decision.get("parallel_group"):
            ids.add(str(decision["parallel_group"]))
    return ids


# ── Settings ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class FanoutSettings:
    """The resolved fan-out gates and per-member budgets."""

    mode: str = "auto-coordinate"
    effective_mode: str = "auto-coordinate"
    width: int = 1
    timeout_s: int = 120
    token_budget: int = 20000
    cost_budget_usd: float = 0.0

    @property
    def parallel_possible(self) -> bool:
        """The mode/width gates for parallel scheduling (the pair
        allowlist and per-turn group cap are judged per group)."""
        return self.effective_mode == "auto-broadcast" and self.width >= 2

    def budget_settings(self) -> subagents.SubagentSettings:
        """The S13-shaped settings the child budget gate reads."""
        return subagents.SubagentSettings(
            enabled=True, max_per_turn=0,
            timeout_s=self.timeout_s, token_budget=self.token_budget,
            cost_budget_usd=self.cost_budget_usd)


def resolve_fanout_settings(user_config: Any | None) -> FanoutSettings:
    """Resolve the fan-out gates from a config object (None = defaults).

    Mode and width come from the delegation-settings resolver (P1e,
    the single read path): parallel scheduling needs the
    ``auto-broadcast`` mode and a width of at least 2, itself capped
    at the Gate-4 recommended width. Budgets are the plan's
    sub-agent budgets applied at this level (120 s / 20k tokens by
    default), with the P1e per-delegation timeout / token budget
    taking precedence when the owner has set them (non-zero).
    """
    resolved = resolve_delegation_settings(user_config)
    base = subagents.subagent_settings(user_config)
    return FanoutSettings(
        mode=resolved.mode,
        effective_mode=resolved.effective_mode,
        width=max(1, min(resolved.max_parallel_delegations, PLAN_MAX_WIDTH)),
        timeout_s=(resolved.per_delegation_timeout_s
                   if resolved.per_delegation_timeout_s > 0
                   else base.timeout_s),
        token_budget=(resolved.per_delegation_token_budget
                      if resolved.per_delegation_token_budget > 0
                      else base.token_budget),
        cost_budget_usd=base.cost_budget_usd,
    )


# ── The group record (D-1 resume state) ───────────────────────────

def _record_db_path() -> Path:
    """The group record lives in the sub-agent checkpoint database.

    Read through the subagents module at call time (never bound at
    import) so the S13 test seam — patching
    ``subagents.SUBAGENT_CHECKPOINT_DB`` — moves the records too.
    """
    return Path(subagents.SUBAGENT_CHECKPOINT_DB)


def _record_connect() -> sqlite3.Connection:
    path = _record_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS parallel_groups ("
        " group_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
    return conn


def _load_record(group_id: str) -> dict[str, Any] | None:
    try:
        with _record_connect() as conn:
            row = conn.execute(
                "SELECT record FROM parallel_groups WHERE group_id = ?",
                (group_id,)).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        return record if isinstance(record, dict) else None
    except Exception:
        logger.debug("Fan-out group record read failed", exc_info=True)
        return None


def _save_record(record: dict[str, Any]) -> None:
    """Persist the group record (best-effort observational state —
    a write failure is logged, never raised into the run; the
    in-flight execution continues on its in-memory copy)."""
    try:
        record["updated_ts"] = time.time()
        with _record_connect() as conn:
            conn.execute(
                "INSERT INTO parallel_groups (group_id, record) "
                "VALUES (?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                "record = excluded.record",
                (record["group_id"], json.dumps(record)))
    except Exception:
        logger.debug("Fan-out group record write failed", exc_info=True)


def _new_record(
    group_id: str,
    state: dict[str, Any],
    members: list[FanoutMember],
) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "task_id": state.get("task_id"),
        "session_id": state.get("session_id"),
        "scheduling": None,
        "degraded_reason": "",
        "reported": False,
        "created_ts": time.time(),
        "updated_ts": time.time(),
        "members": [
            {
                "specialist": m.specialist,
                "task": m.task,
                "tier": m.tier,
                "status": "pending",
                "summary": "",
                "detail": "",
                "duration_ms": 0,
                "tokens_used": 0,
                "pause_payload": None,
            }
            for m in members
        ],
    }


def classify_group(record: dict[str, Any]) -> str:
    """The D-1 classification of a recorded group.

    "skip"   — every member DONE: replay outcomes, re-run nothing.
    "rerun"  — no member started: run the group fresh.
    "resume" — a mix of DONE and NOT DONE (pending / paused):
               run only the unfinished members.
    "ask"    — any member UNCERTAIN: it was mid-run when execution
               stopped, so its tools may or may not have executed;
               the group stops and asks, and that member is never
               silently re-run.
    """
    statuses = [str(m.get("status") or "pending")
                for m in record.get("members") or []]
    if not statuses:
        return "rerun"
    if any(s == "running" for s in statuses):
        return "ask"
    if all(s in FINAL_MEMBER_STATUSES for s in statuses):
        return "skip"
    if all(s == "pending" for s in statuses):
        return "rerun"
    return "resume"


# ── Member execution (the S13 child pattern, segment form) ───────

def _member_thread(parent_thread: str, group_id: str, index: int) -> str:
    short = group_id.rsplit(":", 1)[-1]
    return f"{parent_thread}:fanout:{short}:{index}"


def _member_marker(group_id: str, index: int) -> str:
    """The UsageLedger task_class marking one member's provider calls."""
    short = group_id.rsplit(":", 1)[-1]
    return f"fanout:{short}:{index}"


@dataclass
class _MemberOutcome:
    status: str  # a FINAL_MEMBER_STATUSES member, "paused" or "uncertain"
    values: dict[str, Any] | None = None
    pause_payload: Any = None
    detail: str = ""
    duration_s: float = 0.0


def _derived_member_config(user_config: Any | None,
                           settings: FanoutSettings) -> Any | None:
    """The member's view of the user config: S13's derivation, with
    the resolved fan-out budgets written into the S13 budget fields
    the child gate reads (so a P1e per-delegation timeout/token
    budget, when set, is what the gate enforces)."""
    derived = subagents._derived_user_config(user_config)
    if derived is None:
        return None
    base = subagents.subagent_settings(user_config)
    if (settings.timeout_s == base.timeout_s
            and settings.token_budget == base.token_budget):
        return derived
    try:
        import dataclasses

        return dataclasses.replace(
            derived,
            subagent_timeout_s=settings.timeout_s,
            subagent_token_budget=settings.token_budget)
    except Exception:  # noqa: BLE001 — a non-dataclass config passes through whole.
        return derived


def _member_db_path(group_id: str, index: int) -> Path:
    """One member's checkpoint file: a sibling of the sub-agent
    checkpoint database (see the module docstring for why members
    cannot share one checkpoint file)."""
    base = _record_db_path()
    short = group_id.rsplit(":", 1)[-1]
    return base.with_name(
        f"{base.stem}.fanout-{short}-{index}{base.suffix}")


# One open saver per member checkpoint file, cached for the life
# of the process. Two proven constraints drive this: (1) a member's
# segments span several coordinator passes (the coordinator itself
# interrupts between them), and a FRESH connection's snapshot of a
# just-paused child thread is unreliable with the SQLite saver —
# reads intermittently see an empty thread although the checkpoint
# rows are durable on disk; (2) S13's bridging reads back pauses
# through the same connection that wrote them, which is reliable.
# So each member file gets exactly one connection, created on
# first use and closed when its group reaches a terminal state
# (``_close_group_savers``), which bounds open connections to
# in-flight groups.
# The context manager is cached WITH the saver: dropping the
# manager lets the garbage collector close the connection under
# the saver (from_conn_string is an async-generator context
# manager), so the pair is only ever released by
# _close_group_savers. The guard is a threading.Lock, not an
# asyncio.Lock: a module-level asyncio.Lock binds to the first
# event loop that touches it and breaks every later loop, while
# this cache only needs dict-level atomicity (one member's
# segments are strictly sequential, so same-key creation never
# races).
_MEMBER_SAVERS: dict[str, tuple[Any, AsyncSqliteSaver]] = {}
_MEMBER_SAVER_LOCK = threading.Lock()


async def _member_saver(db_path: Path) -> AsyncSqliteSaver:
    key = str(db_path)
    with _MEMBER_SAVER_LOCK:
        cached = _MEMBER_SAVERS.get(key)
    if cached is not None:
        return cached[1]
    manager = AsyncSqliteSaver.from_conn_string(key)
    saver = await manager.__aenter__()
    await saver.setup()
    with _MEMBER_SAVER_LOCK:
        existing = _MEMBER_SAVERS.setdefault(key, (manager, saver))
    if existing[1] is not saver:
        # Lost a creation race (not expected — see above): keep
        # the registered saver and close this duplicate.
        try:
            await manager.__aexit__(None, None, None)
        except Exception:  # duplicate cleanup only.
            logger.debug(
                "Fan-out duplicate member saver close failed for %s",
                key, exc_info=True)
        return existing[1]
    return saver


async def _close_group_savers(group_id: str, member_count: int) -> None:
    """Close and forget a finished group's member savers."""
    for index in range(member_count):
        key = str(_member_db_path(group_id, index))
        with _MEMBER_SAVER_LOCK:
            cached = _MEMBER_SAVERS.pop(key, None)
        if cached is not None:
            manager, _saver = cached
            try:
                await manager.__aexit__(None, None, None)
            except Exception:  # closing is housekeeping; a failure here strands nothing.
                logger.debug(
                    "Fan-out member saver close failed for %s",
                    key, exc_info=True)


async def _run_member_segment(
    state: dict[str, Any],
    configurable: dict[str, Any],
    *,
    member: FanoutMember,
    index: int,
    group_id: str,
    settings: FanoutSettings,
    deadline: float,
    resume_decision: Any = None,
) -> _MemberOutcome:
    """Run (or resume) one member's child graph for one segment.

    A segment ends when the child completes, fails, exhausts a
    budget, hits its deadline — or pauses for approval, which is
    REPORTED (status "paused", the pending payload attached), never
    bridged here: the coordinator surfaces pauses one at a time, in
    declaration order, through its own interrupt() (see the module
    docstring). Mirrors ``subagents._run_child_attempt``'s run
    discipline minus the bridging loop.
    """
    from aether_engine.langgraph.graph import _build_specialist_graph

    started = time.monotonic()
    parent_thread = (
        configurable.get("thread_id") or state.get("session_id") or "task")
    marker = _member_marker(group_id, index)
    child_configurable = dict(configurable)
    # The child is an independently checkpointed graph (its own
    # file, its own thread): nothing of the parent's Pregel runtime
    # may leak into its config. The runtime keys the supervisor's
    # run injects into every node's configurable (__pregel_* —
    # above all __pregel_checkpointer, the PARENT's saver, and the
    # __pregel_scratchpad) would otherwise adopt the child into the
    # parent's checkpointing under invocation-order namespaces,
    # and the parent's checkpoint coordinates (checkpoint_id /
    # checkpoint_map) name checkpoints in the parent's database.
    # Proven during this slice's build: with the leak, member
    # checkpoints landed in the main database under drifting
    # namespaces and a resumed member restarted with empty
    # channels (it lost even its seeded active_specialist).
    for key in [k for k in child_configurable
                if k.startswith("__pregel_")]:
        child_configurable.pop(key, None)
    child_configurable.pop("checkpoint_id", None)
    child_configurable["checkpoint_ns"] = ""
    child_configurable["checkpoint_map"] = {}
    child_configurable.update({
        "thread_id": _member_thread(parent_thread, group_id, index),
        "user_config": _derived_member_config(
            configurable.get("user_config"), settings),
        "is_subagent_run": True,
        "subagent_marker": marker,
    })
    child_config = {
        "configurable": child_configurable,
        "recursion_limit": _MEMBER_RECURSION_LIMIT,
    }

    def _outcome(status: str, values: dict[str, Any] | None = None,
                 payload: Any = None, detail: str = "") -> _MemberOutcome:
        return _MemberOutcome(
            status=status, values=values, pause_payload=payload,
            detail=detail, duration_s=time.monotonic() - started)

    # The member's own checkpoint file and its process-cached saver
    # (see _member_saver for why one connection per member file is
    # kept open across the group's coordinator passes).
    member_db = _member_db_path(group_id, index)
    member_db.parent.mkdir(parents=True, exist_ok=True)
    saver = await _member_saver(member_db)
    child = _build_specialist_graph(
        member.specialist, subagents._child_node, checkpointer=saver)

    # The group record is the authority on the member's status and
    # D-1 classification lives in the coordinator: a decision in
    # hand means the member is paused and resumes; anything else
    # the coordinator runs is a member that has never run, which
    # starts from its derived initial state. The segment does not
    # re-derive status from the child checkpoint — a snapshot read
    # is not reliable enough to judge by (see _member_saver), and
    # the record already carries the judgement.
    if resume_decision is not None:
        segment: Any = Command(resume=resume_decision)
    else:
        segment = subagents._child_initial_state(
            state, role=member.specialist, task=member.task,
            parent_agent="supervisor", deadline=deadline)
    remaining = deadline - time.time()
    if remaining <= 0:
        return _outcome("timed_out")

    async def _invoke() -> Any:
        with subagents.usage_context(
                session_id=state.get("session_id"),
                task_id=state.get("task_id"),
                task_class=marker):
            return await child.ainvoke(segment, child_config)

    try:
        # The child runs as a ROOT graph — its own checkpointer,
        # thread and namespace — never as a subgraph of the
        # supervisor's run. Invoked in the ambient context,
        # LangGraph adopts the running graph's checkpointer and an
        # invocation-order checkpoint namespace for the child, so
        # the child's checkpoints land in the MAIN database under
        # coordinates that drift between coordinator passes, and a
        # later resume can lose the child outright (all proven
        # during this slice's build). A fresh contextvars context
        # severs the adoption; the usage context is re-entered
        # inside it, so the member's usage rows still carry the
        # parent task's identity. Module-level state the members
        # must share (the ADR-0030 approval cache above all) is not
        # context-scoped and is unaffected.
        invoke_task = asyncio.create_task(
            _invoke(), context=contextvars.Context())
        out = await asyncio.wait_for(invoke_task, timeout=remaining)
    except GraphInterrupt as gi:
        # The child's pause propagating out of the invoke (the
        # native subgraph shape): the pause is read back from the
        # child checkpoint through the same connection that wrote
        # it; the interrupts the exception itself carries are the
        # fallback (S13's two shapes).
        try:
            cstate = await child.aget_state(child_config)
            carried = list(cstate.interrupts) if cstate is not None else []
            cvalues = dict(cstate.values or {}) if cstate is not None else {}
        except Exception:  # noqa: BLE001 — the pause is real even if the read fails.
            carried, cvalues = [], {}
        if carried:
            return _outcome("paused", cvalues, carried[0].value)
        exc_carried = gi.args[0] if gi.args else None
        if isinstance(exc_carried, (list, tuple)) and exc_carried:
            first = exc_carried[0]
            return _outcome(
                "paused", None, getattr(first, "value", first))
        return _outcome("failed", None,
                        detail="the child paused but its interrupt "
                               "could not be read back")
    except TimeoutError:
        return _outcome("timed_out")
    except Exception as exc:  # noqa: BLE001 — child-run boundary: an honest failure outcome, never a raised crash into the group.
        return _outcome(
            "failed", None,
            detail=f"{type(exc).__name__}: {exc}"[:300])
    if isinstance(out, dict) and out.get("__interrupt__"):
        interrupts = out.get("__interrupt__") or []
        payload = interrupts[0].value if interrupts else None
        return _outcome("paused", out, payload)
    return _outcome(subagents._attempt_status(out), out)


# ── Member summaries (the I/O contract back to the supervisor) ────

def _member_summary(
    record_member: dict[str, Any],
    *,
    index: int,
    total: int,
    settings: FanoutSettings,
) -> str:
    """One member's bounded result block (S13 summary-shaped)."""
    role = str(record_member.get("specialist") or "?")
    status = str(record_member.get("status") or "pending")
    task = str(record_member.get("task") or "")
    duration_s = (record_member.get("duration_ms") or 0) / 1000.0
    tokens = int(record_member.get("tokens_used") or 0)
    head = (
        f"Parallel member {index + 1}/{total} — {role}: "
        f"{status.replace('_', ' ')} in {duration_s:.1f}s, "
        f"{tokens:,} tokens recorded.")
    lines = [head, f"Sub-task: {' '.join(task.split())[:TASK_EXCERPT_CHARS]}"]
    summary = str(record_member.get("summary") or "")
    detail = str(record_member.get("detail") or "")
    if status == "completed":
        lines.append("Result:")
        lines.append(summary.strip()[:800] or "(no text result)")
    elif status == "failed":
        lines.append(f"Failure: {detail[:300]}")
        lines.append(
            "No result was produced. Continue without it, take a "
            "different approach, or delegate it again.")
    elif status == "timed_out":
        lines.append(
            f"The member did not finish within the "
            f"{settings.timeout_s}s time budget and was stopped.")
        lines.append("Treat the sub-task as not done.")
    elif status == "budget_exhausted":
        lines.append(summary.strip()[:800] or detail[:300])
        lines.append("Treat the sub-task as not done.")
    elif status == "uncertain":
        lines.append(
            "This member was interrupted mid-run and its state is "
            "UNCERTAIN — its last tool call may or may not have "
            "executed. It was NOT re-run. Ask the user how to "
            "proceed, or delegate the sub-task again explicitly.")
    elif status == "denied":
        lines.append(f"Denied before it ran: {detail[:300]}")
    else:  # pending / paused / running in a recorded outcome
        lines.append("This member did not run to completion.")
    return "\n".join(lines)[:MEMBER_SUMMARY_MAX_CHARS]


def _blackboard_line(record_member: dict[str, Any], index: int) -> str:
    """The one-line digest of one member for the team blackboard."""
    role = str(record_member.get("specialist") or "?")
    status = str(record_member.get("status") or "pending")
    summary = str(record_member.get("summary") or "")
    detail = str(record_member.get("detail") or "")
    if status == "completed":
        digest = " ".join(summary.split())[:240] or "completed with no text result"
        return f"[Parallel {role} #{index + 1}] {digest}"
    if status == "failed":
        return f"[Parallel {role} #{index + 1}] FAILED: {' '.join(detail.split())[:160]}"
    if status == "timed_out":
        return f"[Parallel {role} #{index + 1}] TIMED OUT — no result"
    if status == "budget_exhausted":
        return f"[Parallel {role} #{index + 1}] STOPPED — budget exhausted, no result"
    if status == "uncertain":
        return f"[Parallel {role} #{index + 1}] INTERRUPTED — state uncertain, not re-run"
    return f"[Parallel {role} #{index + 1}] {status.replace('_', ' ').upper()} — no result"


@dataclass
class GroupOutcome:
    """What a group execution hands back to the delegation tool."""

    group_id: str
    tool_text: str
    blackboard_lines: list[str] = field(default_factory=list)
    scheduling: str = "sequential"
    degraded_reason: str = ""
    members: list[dict[str, Any]] = field(default_factory=list)


def _publish_group_activity(
    state: dict[str, Any],
    *,
    group_id: str,
    scheduling: str,
    degraded_reason: str,
    record_members: list[dict[str, Any]],
) -> None:
    """Publish the ONE ``parallel_group`` NODE_ACTIVITY for a group.

    S12-shaped like the sub-agent activity events; the S3 Task
    Ledger tap folds it into the task's ``parallel_groups`` record,
    so the fan-out shows as one visible unit with per-member
    outcomes. Observational: a publish failure never breaks a group.
    """
    task_id = state.get("task_id")
    if not task_id:
        return
    member_rows = [
        {
            "agent": m.get("specialist"),
            "status": m.get("status"),
            "duration_ms": m.get("duration_ms") or 0,
            "tokens_used": m.get("tokens_used") or 0,
        }
        for m in record_members
    ]
    all_done = bool(member_rows) and all(
        m["status"] == "completed" for m in member_rows)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "session_id": state.get("session_id"),
        "from_node": "supervisor",
        "to_node": "parallel_group",
        "action": "parallel_group",
        "reason": degraded_reason,
        "briefing": "",
        "input_payload": {
            "group_id": group_id,
            "members": [
                {"specialist": m.get("specialist"), "task": m.get("task")}
                for m in record_members
            ],
        },
        "output_payload": {
            "group_id": group_id,
            "scheduling": scheduling,
            "degraded_reason": degraded_reason,
            "members": member_rows,
            "status": "completed" if all_done else "partial",
        },
        "status": "completed" if all_done else "partial",
        "timestamp": int(time.time()),
    }
    try:
        event_bus.publish(task_id, Event(
            type=EventType.NODE_ACTIVITY,
            request_id=str(task_id),
            payload=payload,
        ))
    except Exception:
        logger.debug("Parallel-group activity publish failed", exc_info=True)


def _outcome_from_record(
    state: dict[str, Any],
    record: dict[str, Any],
    settings: FanoutSettings,
    *,
    note: str,
) -> GroupOutcome:
    """Build the tool outcome from a group record (skip / ask /
    join share this shaping, so a replayed outcome reads exactly
    like the original)."""
    record_members = record.get("members") or []
    total = len(record_members)
    blocks = [
        _member_summary(m, index=i, total=total, settings=settings)
        for i, m in enumerate(record_members)
    ]
    names = " + ".join(str(m.get("specialist") or "?") for m in record_members)
    done = sum(1 for m in record_members
               if m.get("status") == "completed")
    header = (
        f"Parallel delegation group ({names}) — {done} of {total} "
        f"members completed, run {record.get('scheduling') or 'sequential'}"
        + (f". {note}" if note else ""))
    text = (header + "\n\n" + "\n\n".join(blocks))[:GROUP_TEXT_MAX_CHARS]
    return GroupOutcome(
        group_id=str(record.get("group_id") or ""),
        tool_text=text,
        blackboard_lines=[
            _blackboard_line(m, i) for i, m in enumerate(record_members)],
        scheduling=str(record.get("scheduling") or "sequential"),
        degraded_reason=str(record.get("degraded_reason") or ""),
        members=record_members,
    )


# ── The coordinator ───────────────────────────────────────────────

def _scheduling_for(
    state: dict[str, Any],
    members: list[FanoutMember],
    settings: FanoutSettings,
    group_id: str,
) -> tuple[str, str]:
    """(scheduling, degraded_reason) for one group declaration.

    Parallel only when every gate holds (module docstring); any
    miss degrades to sequential with the reason — never an error.
    """
    reasons: list[str] = []
    if settings.effective_mode != "auto-broadcast":
        reasons.append(
            f"the delegation mode is {settings.effective_mode!r}, "
            f"not 'auto-broadcast'")
    if len(members) > settings.width:
        reasons.append(
            f"the group is {len(members)} wide but parallel width "
            f"resolves to {settings.width} (max_parallel_delegations, "
            f"capped at the Gate-4 width of {PLAN_MAX_WIDTH})")
    pair_reason = validate_group(members)
    if pair_reason:
        reasons.append(pair_reason)
    declared = groups_already_declared(state)
    if group_id not in declared and len(declared) >= PLAN_MAX_GROUPS_PER_TURN:
        reasons.append(
            f"the fan-out cap for one turn ({PLAN_MAX_GROUPS_PER_TURN} "
            f"groups) is already reached")
    if reasons:
        return "sequential", "; ".join(reasons)
    return "parallel", ""


async def run_parallel_group(
    state: dict[str, Any],
    config: dict[str, Any],
    members: list[FanoutMember],
) -> GroupOutcome:
    """Execute one declared parallel group; return its outcome.

    The coordinator behind the ``delegate_in_parallel`` tool. See
    the module docstring for the execution, approval-bridging and
    resume semantics; in short: members run as isolated S13-pattern
    children (concurrently or sequentially per the gates), pauses
    are bridged one at a time in declaration order through this
    function's own ``interrupt()`` calls, and the D-1 group record
    is consulted before anything runs.
    """
    configurable = (config or {}).get("configurable", {}) or {}
    user_config = configurable.get("user_config")
    settings = resolve_fanout_settings(user_config)
    task_id = state.get("task_id")
    group_id = group_id_for(task_id, members)

    # Plan-mode backstop (the hook denies groups in plan mode
    # before the tool ever runs; this covers a forged or stale
    # call reaching the tool directly): plan mode delegates one
    # planner step at a time, never a group.
    if state.get("plan_mode"):
        record = _new_record(group_id, state, members)
        for row in record["members"]:
            row["status"] = "denied"
            row["detail"] = (
                "plan mode is active: parallel delegation groups "
                "are disabled in plan mode")
        record["scheduling"] = "sequential"
        return _outcome_from_record(
            state, record, settings,
            note="The group was not run (plan mode).")

    record = _load_record(group_id)
    if record is None:
        record = _new_record(group_id, state, members)
        _save_record(record)
    classification = classify_group(record)

    if classification == "skip":
        if not record.get("reported"):
            _publish_group_activity(
                state, group_id=group_id,
                scheduling=str(record.get("scheduling") or "sequential"),
                degraded_reason=str(record.get("degraded_reason") or ""),
                record_members=record.get("members") or [])
            record["reported"] = True
            _save_record(record)
        await _close_group_savers(
            group_id, len(record.get("members") or []))
        return _outcome_from_record(
            state, record, settings,
            note="These are the recorded outcomes; the group was "
                 "not re-run.")

    if classification == "ask":
        # D-1: a member was mid-run when execution stopped. Its
        # recorded status flips to the honest UNCERTAIN so the
        # report (and any later re-entry) names it as such; the
        # group stops here and re-runs nothing.
        for row in record.get("members") or []:
            if row.get("status") == "running":
                row["status"] = "uncertain"
        _save_record(record)
        if not record.get("reported"):
            _publish_group_activity(
                state, group_id=group_id,
                scheduling=str(record.get("scheduling") or "sequential"),
                degraded_reason=str(record.get("degraded_reason") or ""),
                record_members=record.get("members") or [])
            record["reported"] = True
            _save_record(record)
        await _close_group_savers(
            group_id, len(record.get("members") or []))
        return _outcome_from_record(
            state, record, settings,
            note="The group was interrupted and one or more members "
                 "are UNCERTAIN; nothing was re-run (D-1).")

    scheduling, degraded_reason = _scheduling_for(
        state, members, settings, group_id)
    record["scheduling"] = scheduling
    record["degraded_reason"] = degraded_reason
    _save_record(record)
    if degraded_reason:
        logger.info(
            "Parallel group %s degraded to sequential: %s",
            group_id, degraded_reason)

    record_members: list[dict[str, Any]] = record["members"]
    decisions_in_hand: dict[int, Any] = {}
    _record_db_path().parent.mkdir(parents=True, exist_ok=True)
    while True:
        record = _load_record(group_id) or record
        record_members = record["members"]
        statuses = [str(m.get("status") or "pending")
                    for m in record_members]
        if all(s in FINAL_MEMBER_STATUSES for s in statuses):
            break
        paused_idx = [i for i, s in enumerate(statuses) if s == "paused"]
        runnable = [
            i for i, s in enumerate(statuses)
            if s == "pending" or i in decisions_in_hand
        ]
        if paused_idx and paused_idx[0] not in decisions_in_hand:
            # Surface the earliest-declared waiting pause. The
            # budget gate runs first (S13 discipline): a member
            # already over budget is finalised, never surfaced
            # for an approval its work cannot use.
            i = paused_idx[0]
            stop = await subagents._budget_stop(
                task_id, _member_marker(group_id, i),
                settings.budget_settings())
            if stop is not None:
                kind, tokens, cost = stop
                row = record_members[i]
                row["status"] = (
                    "timed_out" if kind == subagents.STOP_DEADLINE
                    else "budget_exhausted")
                row["summary"] = subagents._stop_text(
                    kind, settings.budget_settings(), tokens, cost)
                row["tokens_used"] = tokens
                _save_record(record)
                continue
            decision = interrupt(record_members[i].get("pause_payload"))
            decisions_in_hand[i] = decision
            continue
        if not runnable:
            # Nothing runnable and nothing to surface: every
            # remaining member is paused with its decision in
            # hand — impossible by construction (an in-hand
            # decision makes its member runnable); break
            # defensively rather than spin.
            break
        for i in runnable:
            if record_members[i].get("status") == "pending":
                record_members[i]["status"] = "running"
        _save_record(record)
        deadline = time.time() + settings.timeout_s

        async def _segment(
                i: int, seg_deadline: float) -> tuple[int, _MemberOutcome]:
            member = members[i]
            outcome = await _run_member_segment(
                state, configurable,
                member=member, index=i, group_id=group_id,
                settings=settings, deadline=seg_deadline,
                resume_decision=decisions_in_hand.get(i))
            return i, outcome

        if scheduling == "parallel" and len(runnable) > 1:
            results = await asyncio.gather(
                *[_segment(i, deadline) for i in runnable],
                return_exceptions=True)
        else:
            results = [await _segment(i, deadline) for i in runnable]
        for res in results:
            if isinstance(res, Exception):
                logger.warning(
                    "Fan-out member segment raised unexpectedly: %s",
                    res)
                continue
            i, outcome = res
            decisions_in_hand.pop(i, None)
            row = record_members[i]
            row["duration_ms"] = int(
                (row.get("duration_ms") or 0)
                + outcome.duration_s * 1000)
            tokens, _cost = subagents.subagent_usage(
                task_id, _member_marker(group_id, i))
            row["tokens_used"] = tokens
            if outcome.status == "paused":
                row["status"] = "paused"
                row["pause_payload"] = outcome.pause_payload
                continue
            if outcome.status == "uncertain":
                row["status"] = "uncertain"
                row["detail"] = (
                    "interrupted mid-run; state uncertain (D-1)")
                continue
            row["status"] = outcome.status
            row["pause_payload"] = None
            result_text, _tools_used = subagents._child_result(
                outcome.values)
            row["summary"] = result_text
            if outcome.detail:
                row["detail"] = outcome.detail
        _save_record(record)

    record = _load_record(group_id) or record
    _publish_group_activity(
        state, group_id=group_id, scheduling=scheduling,
        degraded_reason=degraded_reason,
        record_members=record.get("members") or [])
    record["reported"] = True
    _save_record(record)
    await _close_group_savers(
        group_id, len(record.get("members") or []))
    return _outcome_from_record(
        state, record, settings,
        note=(f"Degraded to sequential execution: {degraded_reason}."
              if degraded_reason else ""))


# ── The supervisor tool ───────────────────────────────────────────

def build_parallel_group_tool():
    """The ``delegate_in_parallel`` tool (tools topology only).

    Bound beside the P1d delegation tools; offered by the model
    adapter under the same availability gating as a delegation.
    Returns the delegation tools' two-Command shape: the joined
    result as a single tool message first (a batched write would be
    dict-converted by the state reducer — see delegation.py), then
    the blackboard digest lines.
    """

    @tool(
        GROUP_TOOL_NAME,
        description=(
            "Delegate TWO independent sub-tasks in parallel — one "
            "to the researcher and one to the planner, or two "
            "independent questions to the researcher. Each member "
            "receives ONLY its own 'task' text plus the shared team "
            "blackboard (not the conversation), so state each task "
            "completely. Members run at the same time and their "
            "results return together, in the order declared. Use "
            "only for genuinely independent work: never for the "
            "coder, and never when one sub-task needs the other's "
            "result — delegate those one at a time instead. "
            "'members' is a list of {specialist, task} objects."
        ),
    )
    async def _delegate_parallel(
            members: list[dict], runtime: ToolRuntime) -> list[Command]:
        state = dict(runtime.state or {})
        parsed, error = parse_group_members(members)
        if error is not None:
            # The hook refuses malformed declarations before the
            # tool runs; this covers a call that reaches the tool
            # without the hook (a forged or stale pending call).
            return [Command(update={"messages": [ToolMessage(
                content=(
                    f"Error: the parallel group declaration is "
                    f"invalid: {error}. No specialist was started."),
                name=GROUP_TOOL_NAME,
                tool_call_id=runtime.tool_call_id,
            )]})]
        outcome = await run_parallel_group(
            state, runtime.config or {}, parsed)
        commands = [Command(update={"messages": [ToolMessage(
            content=outcome.tool_text,
            name=GROUP_TOOL_NAME,
            tool_call_id=runtime.tool_call_id,
        )]})]
        if outcome.blackboard_lines:
            commands.append(Command(update={
                "blackboard": {"findings": outcome.blackboard_lines},
            }))
        return commands

    return _delegate_parallel
