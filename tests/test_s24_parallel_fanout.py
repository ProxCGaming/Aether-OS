"""S24 contract + unit tests: parallel fan-out (P2b, K-12).

The supervisor may declare a parallel group — a pair of
independent delegations emitted together — and the members run as
isolated S13-pattern children, joining in declaration order. The
contract pinned here:

- a researcher+planner group completes with both results in the
  supervisor's view, both delegations counted (a group of N costs
  N from the delegation cap), per-member delegation activity, and
  ONE ``parallel_group`` ledger record with per-member outcomes;
- with fan-out enabled the members genuinely overlap in time;
  with it off (the default) the same declaration executes
  sequentially in declaration order with the same results;
- approval pauses inside members serialise globally, FIFO in
  declaration order — never doubled, never lost;
- a member's timeout / budget stop never fails the group;
- the D-1 resume classification: DONE replays, NOT DONE runs,
  UNCERTAIN stops and asks and is never silently re-run.

Providers are scripted per node name, on the P1d characterisation
harness's pattern. A supervisor ("group", members) action natively
calls whichever ``delegate_in_parallel`` tool the run offers — it
exists only on the tools topology.
"""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from aether_common.contracts import EventType, TaskState
from aether_engine import task_ledger
from aether_engine.config import UserConfig
from aether_engine.cost.ledger import UsageLedger, record_provider_call
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph import fanout
from aether_engine.langgraph import subagents as subagents_module
from aether_engine.langgraph.fanout import (
    GROUP_TOOL_NAME,
    FanoutMember,
    classify_group,
    group_id_for,
    parse_group_members,
    resolve_fanout_settings,
    run_parallel_group,
    validate_group,
)
from aether_engine.langgraph.graph import compile_graph
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.state import message_dicts
from aether_engine.langgraph.supervisor_model import (
    _parallel_group_policy_denial,
    supervisor_post_model_hook,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

RESEARCHER_ANSWER = "Research findings: the fan-out works."
PLANNER_ANSWER = "Plan: 1) fan out, 2) join in order."
CODER_ANSWER = "Implementation complete: coded sequentially."
PROBE_RESULT = "fan-probe-result"

GROUP_MEMBERS = [
    {"specialist": "researcher", "task": "research the first question"},
    {"specialist": "planner", "task": "plan the second question"},
]


def _broadcast_config(**overrides):
    """A UserConfig with fan-out enabled (Gate-4 defaults)."""
    block = {"mode": "auto-broadcast", "max_parallel_delegations": 2}
    block.update(overrides.pop("delegation_block", {}))
    return UserConfig(delegation_settings=block, **overrides)


class FanoutProvider:
    """One provider for every node, scripted per node name.

    Supervisor actions: ("group", members) / ("native", target) /
    ("json", decision) / ("text", content). Specialist actions:
    ("text", content) / ("timed", delay, content) /
    ("tool", name, args). Timing intervals are recorded for the
    concurrency proofs.
    """

    provider_name = "scripted"
    model = "scripted-model"

    def __init__(self, supervisor_script, specialist_scripts=None):
        self.supervisor_script = list(supervisor_script)
        self.specialist_scripts = {
            name: list(actions)
            for name, actions in (specialist_scripts or {}).items()
        }
        self.calls: list[dict] = []
        self.timing: list[tuple[str, float, float]] = []

    def rounds(self, node_name):
        return sum(1 for c in self.calls if c["node_name"] == node_name)

    def intervals(self, node_name):
        return [(s, e) for n, s, e in self.timing if n == node_name]

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        offered = [
            t.get("function", {}).get("name")
            for t in (tools or [])
            if isinstance(t, dict)
        ]
        self.calls.append({
            "node_name": node_name,
            "messages": list(messages),
            "tools": offered,
        })
        if node_name == "supervisor":
            action = (
                self.supervisor_script.pop(0)
                if self.supervisor_script
                else ("json", {"next": "END", "reason": "script done",
                               "briefing": ""})
            )
            kind = action[0]
            if kind == "json":
                import json as _json
                yield StreamChunk(text=_json.dumps(action[1]),
                                  finish_reason="stop")
            elif kind == "text":
                yield StreamChunk(text=action[1], finish_reason="stop")
            elif kind == "group":
                assert GROUP_TOOL_NAME in offered, (
                    f"group tool not offered: {offered}")
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=GROUP_TOOL_NAME,
                        args={"members": action[1]},
                        call_id=f"group-{self.rounds('supervisor')}")],
                    finish_reason="tool_calls")
            elif kind == "native":
                target = action[1]
                name = next(
                    (n for n in offered
                     if n and n.endswith(target)
                     and n.startswith(("transfer_to_", "delegate_to_"))),
                    None,
                )
                assert name, f"no delegation tool offered for {target}"
                args = {"briefing": "native briefing"} \
                    if name.startswith("delegate_to_") else {}
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=name, args=args,
                        call_id=f"native-{target}-{self.rounds('supervisor')}")],
                    finish_reason="tool_calls")
            else:  # pragma: no cover - test authoring error
                raise AssertionError(f"unknown supervisor action {action}")
            return

        actions = self.specialist_scripts.get(node_name) or []
        action = actions.pop(0) if actions else ("text", f"{node_name} answer")
        if action[0] == "timed":
            _, delay, content = action
            start = time.monotonic()
            await asyncio.sleep(delay)
            self.timing.append((node_name, start, time.monotonic()))
            yield StreamChunk(text=content, finish_reason="stop")
        elif action[0] == "tool":
            yield StreamChunk(
                tool_calls=[ToolCall(
                    name=action[1], args=action[2] or {},
                    call_id=f"{node_name}-tool-{self.rounds(node_name)}")],
                finish_reason="tool_calls")
        else:
            yield StreamChunk(text=action[1], finish_reason="stop")


@pytest.fixture
def fanout_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subagents_module, "SUBAGENT_CHECKPOINT_DB",
        tmp_path / "subagent_checkpoints.db")
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    for module in (researcher_module, planner_module, coder_module):
        monkeypatch.setattr(
            module, "save_artifact", lambda *a, **k: "/tmp/fanout_artifact.txt")
    task_ledger.clear_tap()
    return tmp_path


def _initial_state(prompt="do the work", **extra):
    state = {
        "original_prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
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
        "plan_mode": False,
        "delegation_log": [],
        "delegation_count": 0,
        "subagent_count": 0,
        "task_id": "fanout-task",
        "session_id": "fanout-session",
        "message_id": None,
        "context_briefing": None,
        "active_skills": [],
        "run_config": None,
    }
    state.update(extra)
    return state


def _config(provider, *, user_config=None, registry=None):
    return {
        "recursion_limit": 25,
        "configurable": {
            "thread_id": "fanout-thread",
            "provider": provider,
            "tool_registry": registry or ToolRegistry(),
            "user_config": user_config or UserConfig(),
            "agent_models": {},
            "agent_tools": {},
            "disabled_nodes": set(),
            "workspace_roots": [],
            "request_id": "fanout-req",
            "session_id": "fanout-session",
            "task_class": "standard",
            "forced_agent": None,
        },
    }


async def _run_graph(tmp_path, provider, *, user_config=None, registry=None,
                     pattern="tools", state=None):
    db_path = tmp_path / "main.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern=pattern)
        return await graph.ainvoke(
            state or _initial_state(),
            config=_config(provider, user_config=user_config,
                           registry=registry))


def _decisions(final):
    return [
        entry["decision"]["next"]
        for entry in final.get("delegation_log") or []
        if isinstance(entry, dict) and isinstance(entry.get("decision"), dict)
    ]


def _group_tool_text(final):
    for msg in message_dicts(final.get("messages") or []):
        if msg.get("role") == "tool" and msg.get("name") == GROUP_TOOL_NAME:
            return str(msg.get("content") or "")
    return ""


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


# ── Unit: parsing + validation ────────────────────────────────────

def test_parse_group_members():
    members, error = parse_group_members(GROUP_MEMBERS)
    assert error is None
    assert [m.specialist for m in members] == ["researcher", "planner"]
    assert members[0].task == "research the first question"

    members, error = parse_group_members("not-a-list")
    assert members == [] and "non-empty list" in error
    members, error = parse_group_members(
        [{"specialist": "wizard", "task": "x"}])
    assert members == [] and "unknown specialist" in error
    members, error = parse_group_members(
        [{"specialist": "researcher", "task": "  "}])
    assert members == [] and "names no task" in error
    # A P1a tier tag, when present, is carried through untouched.
    members, error = parse_group_members(
        [{"specialist": "researcher", "task": "x", "tier": "strong"}])
    assert error is None and members[0].tier == "strong"


def test_validate_group_pairs():
    def names(*specialists):
        return [FanoutMember(specialist=s, task="t") for s in specialists]

    assert validate_group(names("researcher", "planner")) is None
    assert validate_group(names("researcher", "researcher")) is None
    assert "coder is excluded" in validate_group(names("researcher", "coder"))
    assert "pairs of researcher/planner" in validate_group(
        names("planner", "planner"))
    assert "at least two" in validate_group(names("researcher"))
    assert validate_group(names("researcher", "planner", "researcher")) \
        is not None


# ── Unit: settings resolution ─────────────────────────────────────

def test_resolve_fanout_settings_defaults_off():
    settings = resolve_fanout_settings(UserConfig())
    assert settings.effective_mode == "auto-coordinate"
    assert settings.width == 1
    assert settings.parallel_possible is False
    assert settings.timeout_s == 120
    assert settings.token_budget == 20000


def test_resolve_fanout_settings_broadcast_and_overrides():
    settings = resolve_fanout_settings(_broadcast_config())
    assert settings.effective_mode == "auto-broadcast"
    assert settings.width == 2
    assert settings.parallel_possible is True

    # The width never exceeds the Gate-4 cap, however wide the
    # owner sets max_parallel_delegations.
    wide = resolve_fanout_settings(_broadcast_config(
        delegation_block={"max_parallel_delegations": 16}))
    assert wide.width == fanout.PLAN_MAX_WIDTH

    # P1e per-delegation budgets take precedence over the S13
    # defaults when set.
    budgeted = resolve_fanout_settings(_broadcast_config(
        delegation_block={
            "per_delegation_timeout_s": 45,
            "per_delegation_token_budget": 5000,
        }))
    assert budgeted.timeout_s == 45
    assert budgeted.token_budget == 5000


# ── Unit: the post-model hook's group bookkeeping ─────────────────

def _hook_state(members, *, decision=None, **extra):
    call = {
        "name": GROUP_TOOL_NAME,
        "args": {"members": members},
        "id": "group-call-1",
        "type": "tool_call",
    }
    message = AIMessage(
        content="",
        tool_calls=[call],
        additional_kwargs={"aether_decision": decision or {
            "next": "parallel_group", "reason": "both at once",
            "briefing": "",
        }},
    )
    state = _initial_state()
    state["messages"] = [
        {"role": "user", "content": "do the work"}, message]
    state.update(extra)
    return state


def test_hook_counts_group_as_n_delegations():
    state = _hook_state(GROUP_MEMBERS)
    update = supervisor_post_model_hook(state)
    assert update["delegation_count"] == 2
    entries = update["delegation_log"]
    assert len(entries) == 2
    gid = group_id_for("fanout-task", parse_group_members(GROUP_MEMBERS)[0])
    assert [e["decision"]["next"] for e in entries] == [
        "researcher", "planner"]
    assert all(
        e["decision"]["parallel_group"] == gid for e in entries)
    assert [e["decision"]["parallel_index"] for e in entries] == [0, 1]
    assert update["active_specialist"] == "researcher"


def test_group_policy_cap_judged_for_the_whole_group():
    # The cap needs the run config (the hook reads it via
    # get_config inside a graph run — the graph-level cap test
    # below covers the full path); here the policy function is
    # judged directly with a fabricated config.
    members, _ = parse_group_members(GROUP_MEMBERS)
    config = {"configurable": {
        "user_config": UserConfig(supervisor_delegation_cap=8)}}
    # 7 of 8 used: a group of 2 does not fit — denied as a unit.
    reason = _parallel_group_policy_denial(
        _initial_state(delegation_count=7), members, config=config)
    assert reason and "cap" in reason
    # 6 of 8 used: the group fits exactly — allowed.
    assert _parallel_group_policy_denial(
        _initial_state(delegation_count=6), members,
        config=config) is None
    # The spawn allow-list is judged per member.
    allow_config = {"configurable": {
        "user_config": UserConfig(supervisor_spawn_allowlist=["planner"])}}
    reason = _parallel_group_policy_denial(
        _initial_state(), members, config=allow_config)
    assert reason and "allow-list" in reason


def test_hook_group_plan_mode_denial():
    state = _hook_state(GROUP_MEMBERS, plan_mode=True)
    update = supervisor_post_model_hook(state)
    assert "delegation_count" not in update
    entry = update["delegation_log"][0]
    assert entry["decision"]["denied"] is True
    assert "plan mode" in entry["decision"]["reason"]


def test_hook_group_malformed_denial():
    state = _hook_state([{"specialist": "wizard", "task": "x"}])
    update = supervisor_post_model_hook(state)
    entry = update["delegation_log"][0]
    assert entry["decision"]["denied"] is True
    assert "malformed" in entry["decision"]["reason"]


# ── Unit: D-1 classification + the group record ───────────────────

def _record_with(statuses):
    return {
        "group_id": "g",
        "members": [
            {"specialist": "researcher", "task": "t", "status": s}
            for s in statuses
        ],
    }


def test_classify_group_mixes():
    assert classify_group(_record_with(["completed", "failed"])) == "skip"
    assert classify_group(_record_with(["pending", "pending"])) == "rerun"
    assert classify_group(_record_with(["completed", "pending"])) == "resume"
    assert classify_group(_record_with(["completed", "paused"])) == "resume"
    assert classify_group(_record_with(["completed", "running"])) == "ask"
    assert classify_group(_record_with(["running", "pending"])) == "ask"


def test_group_record_round_trip(fanout_env):
    members, _ = parse_group_members(GROUP_MEMBERS)
    gid = group_id_for("fanout-task", members)
    assert gid == group_id_for("fanout-task", members)  # deterministic
    record = fanout._new_record(gid, _initial_state(), members)
    record["members"][0]["status"] = "completed"
    fanout._save_record(record)
    loaded = fanout._load_record(gid)
    assert loaded["members"][0]["status"] == "completed"
    assert loaded["members"][1]["status"] == "pending"


def test_scheduling_group_cap_degrades():
    members, _ = parse_group_members(GROUP_MEMBERS)
    settings = resolve_fanout_settings(_broadcast_config())
    state = _initial_state(delegation_log=[
        {"agent": "supervisor", "decision": {
            "next": "researcher", "parallel_group": "other-group-1"}},
        {"agent": "supervisor", "decision": {
            "next": "planner", "parallel_group": "other-group-2"}},
    ])
    scheduling, reason = fanout._scheduling_for(
        state, members, settings, group_id_for("fanout-task", members))
    assert scheduling == "sequential"
    assert "fan-out cap" in reason


# ── Coordinator: budgets, resume (direct, no supervisor graph) ────

def _coordinator_config(provider, user_config, registry=None):
    return _config(provider, user_config=user_config, registry=registry)


async def test_coordinator_timeout_member_does_not_fail_group(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[],
        specialist_scripts={
            "researcher": [("timed", 3.0, "slow research")],
            "planner": [("text", PLANNER_ANSWER)],
        })
    members, _ = parse_group_members(GROUP_MEMBERS)
    config = _coordinator_config(
        provider, _broadcast_config(subagent_timeout_s=1))
    started = time.monotonic()
    outcome = await run_parallel_group(
        _initial_state(), config, members)
    elapsed = time.monotonic() - started
    statuses = {m["specialist"]: m["status"] for m in outcome.members}
    assert statuses == {"researcher": "timed_out", "planner": "completed"}
    assert elapsed < 2.5, "the timed-out member must not stall the join"
    assert PLANNER_ANSWER in outcome.tool_text
    assert outcome.scheduling == "parallel"


async def test_coordinator_token_budget_stops_one_member(
        fanout_env, monkeypatch):
    ledger = UsageLedger(fanout_env / "usage.db")
    monkeypatch.setattr(
        "aether_engine.cost.ledger.get_usage_ledger", lambda: ledger)
    monkeypatch.setattr(
        subagents_module, "get_usage_ledger", lambda: ledger)
    executed = []
    registry = ToolRegistry()
    registry.register(Tool(
        name="fan_lookup",
        description="Read-only lookup.",
        input_schema={"type": "object", "properties": {}},
        execute_fn=lambda **kw: executed.append(kw) or "note-result",
        risk=ToolRisk.READ_ONLY,
    ))

    class RecordingProvider(FanoutProvider):
        async def call_stream(self, messages, tools=None,
                              node_name=None, **kwargs):
            if node_name == "researcher" and self.rounds("researcher") == 0:
                self.calls.append({"node_name": node_name,
                                   "messages": list(messages),
                                   "tools": []})
                record_provider_call(
                    model_name="fake-model", provider_name="fake",
                    input_tokens=15000, output_tokens=10000,
                    agent_name="researcher", messages=[], output_text="x")
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="fan_lookup", args={}, call_id="fan-lookup-1")],
                    finish_reason="tool_calls")
                return
            async for chunk in super().call_stream(
                    messages, tools=tools, node_name=node_name, **kwargs):
                yield chunk

    provider = RecordingProvider(
        supervisor_script=[],
        specialist_scripts={
            "researcher": [("text", "research that must never be read")],
            "planner": [("text", PLANNER_ANSWER)],
        })
    members, _ = parse_group_members(GROUP_MEMBERS)
    outcome = await run_parallel_group(
        _initial_state(),
        _coordinator_config(provider, _broadcast_config(), registry),
        members)
    statuses = {m["specialist"]: m["status"] for m in outcome.members}
    assert statuses["researcher"] == "budget_exhausted"
    assert statuses["planner"] == "completed"
    # The budget gate binds between model calls (the S13 rule):
    # the round-1 tool call the member had already emitted runs,
    # but there is no second model call — the member stops instead
    # of reading its own tool result and continuing.
    assert len(executed) == 1
    assert provider.rounds("researcher") == 1
    assert "research that must never be read" not in outcome.tool_text
    assert "token budget" in outcome.tool_text


async def test_coordinator_uncertain_member_stops_and_asks(fanout_env):
    provider = FanoutProvider(supervisor_script=[], specialist_scripts={
        "researcher": [("text", RESEARCHER_ANSWER)],
        "planner": [("text", PLANNER_ANSWER)],
    })
    members, _ = parse_group_members(GROUP_MEMBERS)
    gid = group_id_for("fanout-task", members)
    record = fanout._new_record(gid, _initial_state(), members)
    record["members"][0]["status"] = "completed"
    record["members"][0]["summary"] = "recorded research outcome"
    record["members"][1]["status"] = "running"  # cut off mid-run
    fanout._save_record(record)

    outcome = await run_parallel_group(
        _initial_state(),
        _coordinator_config(provider, _broadcast_config()),
        members)
    assert provider.rounds("researcher") == 0, "DONE is never re-run"
    assert provider.rounds("planner") == 0, "UNCERTAIN is never re-run"
    assert "recorded research outcome" in outcome.tool_text
    planner_block = outcome.tool_text.split("member 2/2")[1]
    assert "UNCERTAIN" in planner_block


async def test_coordinator_resume_runs_only_unfinished(fanout_env):
    provider = FanoutProvider(supervisor_script=[], specialist_scripts={
        "researcher": [("text", RESEARCHER_ANSWER)],
        "planner": [("text", PLANNER_ANSWER)],
    })
    members, _ = parse_group_members(GROUP_MEMBERS)
    gid = group_id_for("fanout-task", members)
    record = fanout._new_record(gid, _initial_state(), members)
    record["members"][0]["status"] = "completed"
    record["members"][0]["summary"] = "recorded research outcome"
    fanout._save_record(record)

    outcome = await run_parallel_group(
        _initial_state(),
        _coordinator_config(provider, _broadcast_config()),
        members)
    assert provider.rounds("researcher") == 0, "DONE is replayed, not re-run"
    assert provider.rounds("planner") == 1, "NOT DONE runs"
    assert "recorded research outcome" in outcome.tool_text
    assert PLANNER_ANSWER in outcome.tool_text


async def test_coordinator_sequential_when_flag_off(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[],
        specialist_scripts={
            "researcher": [("timed", 0.3, RESEARCHER_ANSWER)],
            "planner": [("timed", 0.3, PLANNER_ANSWER)],
        })
    members, _ = parse_group_members(GROUP_MEMBERS)
    outcome = await run_parallel_group(
        _initial_state(),
        _coordinator_config(provider, UserConfig()),
        members)
    assert outcome.scheduling == "sequential"
    assert "auto-broadcast" in outcome.degraded_reason
    statuses = {m["specialist"]: m["status"] for m in outcome.members}
    assert statuses == {"researcher": "completed", "planner": "completed"}
    r = provider.intervals("researcher")[0]
    p = provider.intervals("planner")[0]
    assert not _overlaps(r, p), "flag off must execute sequentially"
    assert r[1] <= p[0], "sequential execution follows declaration order"


# ── Graph-level: the group through the tools supervisor ───────────

async def test_group_completes_in_parallel_declaration_order_join(
        fanout_env):
    provider = FanoutProvider(
        supervisor_script=[("group", GROUP_MEMBERS), ("text", "All done.")],
        specialist_scripts={
            # The planner finishes first; the join must still be in
            # declaration order (researcher first).
            "researcher": [("timed", 0.4, RESEARCHER_ANSWER)],
            "planner": [("timed", 0.05, PLANNER_ANSWER)],
        })
    final = await _run_graph(
        fanout_env, provider, user_config=_broadcast_config())
    assert final["delegation_count"] == 2, "a group of 2 costs 2"
    assert _decisions(final) == ["researcher", "planner", "END"]
    text = _group_tool_text(final)
    assert RESEARCHER_ANSWER in text and PLANNER_ANSWER in text
    assert text.index("member 1/2 — researcher") \
        < text.index("member 2/2 — planner")
    r = provider.intervals("researcher")[0]
    p = provider.intervals("planner")[0]
    assert _overlaps(r, p), "enabled fan-out must actually overlap"
    record = fanout._load_record(group_id_for(
        "fanout-task", parse_group_members(GROUP_MEMBERS)[0]))
    assert record["scheduling"] == "parallel"
    assert [m["status"] for m in record["members"]] == [
        "completed", "completed"]


async def test_group_flag_off_runs_sequentially_same_results(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[("group", GROUP_MEMBERS), ("text", "All done.")],
        specialist_scripts={
            "researcher": [("timed", 0.3, RESEARCHER_ANSWER)],
            "planner": [("timed", 0.3, PLANNER_ANSWER)],
        })
    final = await _run_graph(
        fanout_env, provider, user_config=UserConfig())
    assert final["delegation_count"] == 2
    text = _group_tool_text(final)
    assert RESEARCHER_ANSWER in text and PLANNER_ANSWER in text
    r = provider.intervals("researcher")[0]
    p = provider.intervals("planner")[0]
    assert not _overlaps(r, p)
    record = fanout._load_record(group_id_for(
        "fanout-task", parse_group_members(GROUP_MEMBERS)[0]))
    assert record["scheduling"] == "sequential"
    assert "auto-broadcast" in record["degraded_reason"]


async def test_group_with_coder_degrades_to_sequential(fanout_env):
    members = [
        {"specialist": "researcher", "task": "research it"},
        {"specialist": "coder", "task": "code it"},
    ]
    provider = FanoutProvider(
        supervisor_script=[("group", members), ("text", "All done.")],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "coder": [("text", CODER_ANSWER)],
        })
    final = await _run_graph(
        fanout_env, provider, user_config=_broadcast_config())
    assert final["delegation_count"] == 2
    text = _group_tool_text(final)
    assert RESEARCHER_ANSWER in text and CODER_ANSWER in text
    record = fanout._load_record(group_id_for(
        "fanout-task", parse_group_members(members)[0]))
    assert record["scheduling"] == "sequential"
    assert "coder is excluded" in record["degraded_reason"]


async def test_group_cap_denied_at_the_hook(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[("group", GROUP_MEMBERS)],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "planner": [("text", PLANNER_ANSWER)],
        })
    final = await _run_graph(
        fanout_env, provider,
        user_config=_broadcast_config(supervisor_delegation_cap=1))
    assert final["delegation_count"] == 0
    assert provider.rounds("researcher") == 0
    assert provider.rounds("planner") == 0
    entries = [
        e for e in final.get("delegation_log") or []
        if (e.get("decision") or {}).get("next") == "parallel_group"
    ]
    assert entries and entries[0]["decision"]["denied"] is True
    contents = [str(m.get("content") or "")
                for m in message_dicts(final.get("messages") or [])]
    assert any("blocked by the spawn policy" in c for c in contents)


async def test_approval_pauses_serialise_fifo(fanout_env):
    executed = []

    def _probe(q: str = "") -> str:
        executed.append(q)
        return PROBE_RESULT

    registry = ToolRegistry()
    registry.register(Tool(
        name="fan_probe",
        description="Approval-gated probe.",
        input_schema={"type": "object",
                      "properties": {"q": {"type": "string"}}},
        execute_fn=_probe,
        risk=ToolRisk.READ_ONLY,
    ))
    provider = FanoutProvider(
        supervisor_script=[("group", GROUP_MEMBERS), ("text", "All done.")],
        specialist_scripts={
            "researcher": [
                ("tool", "fan_probe", {"q": "research"}),
                ("text", RESEARCHER_ANSWER)],
            "planner": [
                ("tool", "fan_probe", {"q": "plan"}),
                ("text", PLANNER_ANSWER)],
        })
    user_config = _broadcast_config(
        tool_policies={"fan_probe": "Require Approval"})
    db_path = fanout_env / "main.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(provider, user_config=user_config,
                         registry=registry)
        first = await graph.ainvoke(_initial_state(), config=config)
        interrupts = first.get("__interrupt__") or []
        assert len(interrupts) == 1, "one pause at a time, globally"
        payload = interrupts[0].value
        assert payload.get("type") == "approval_request"
        assert payload.get("tool_name") == "fan_probe"
        assert payload.get("args", {}).get("q") == "research", \
            "pauses surface FIFO in declaration order"

        second = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
        interrupts = second.get("__interrupt__") or []
        assert len(interrupts) == 1, "the sibling's pause is never lost"
        assert interrupts[0].value.get("args", {}).get("q") == "plan"

        final = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
    assert "__interrupt__" not in final
    assert executed == ["research", "plan"], \
        "each approved tool executes exactly once, in order"
    assert final["delegation_count"] == 2
    text = _group_tool_text(final)
    assert RESEARCHER_ANSWER in text and PLANNER_ANSWER in text


# ── Legacy topology: no group exists there ────────────────────────

async def test_legacy_topology_cannot_declare_a_group(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[
            ("json", {"next": "researcher", "reason": "gather facts",
                      "briefing": ""}),
            ("json", {"next": "END", "reason": "done", "briefing": ""}),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run_graph(
        fanout_env, provider, pattern="legacy",
        user_config=_broadcast_config())
    assert final["delegation_count"] == 1
    offered = [
        name
        for call in provider.calls if call["node_name"] == "supervisor"
        for name in call["tools"]
    ]
    assert GROUP_TOOL_NAME not in offered
    assert _group_tool_text(final) == ""


def test_group_teaching_only_in_tools_prompt():
    from aether_engine.langgraph.nodes.supervisor import (
        _supervisor_system_prompt,
    )

    tools_prompt = _supervisor_system_prompt(
        "p", "plan", ["researcher", "planner", "coder"],
        tool_mode=True, delegation_tools=True)
    legacy_prompt = _supervisor_system_prompt(
        "p", "plan", ["researcher", "planner", "coder"], tool_mode=True)
    json_prompt = _supervisor_system_prompt(
        "p", "plan", ["researcher", "planner", "coder"])
    assert GROUP_TOOL_NAME in tools_prompt
    assert GROUP_TOOL_NAME not in legacy_prompt
    assert GROUP_TOOL_NAME not in json_prompt


# ── S24 contract: the group through the executor + ledger ─────────

async def test_executor_group_contract(fanout_env):
    provider = FanoutProvider(
        supervisor_script=[("group", GROUP_MEMBERS), ("text", "All done.")],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "planner": [("text", PLANNER_ANSWER)],
        })
    user_config = _broadcast_config(supervisor_pattern="tools")
    events = [ev async for ev in executor_module.run_langgraph_task(
        "do the work",
        provider,
        tools=ToolRegistry(),
        task_id="task-s24-group",
        request_id="req-s24-group",
        session_id="s24-session",
        user_config=user_config,
    )]
    types = [ev.type for ev in events]
    assert EventType.TASK_FAILED not in types
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert RESEARCHER_ANSWER in completed.payload["response"]
    assert PLANNER_ANSWER in completed.payload["response"]

    delegations = [
        ev.payload for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and (ev.payload or {}).get("action") == "delegation"
    ]
    assert [d["to_node"] for d in delegations] == ["researcher", "planner"]
    assert [d["delegation_count"] for d in delegations] == [1, 2]

    tap = task_ledger.tap_snapshot("task-s24-group")
    assert tap is not None
    groups = tap.get("parallel_groups") or []
    assert len(groups) == 1, "the fan-out is ONE ledger unit"
    assert groups[0]["scheduling"] == "parallel"
    assert [(m["agent"], m["status"]) for m in groups[0]["members"]] == [
        ("researcher", "completed"), ("planner", "completed")]
