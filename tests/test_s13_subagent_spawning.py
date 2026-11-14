"""Master build S13: bounded sub-agent spawning (audit G1 + G2).

A specialist may spawn a bounded sub-agent for a focused sub-task.
The bounds are enforced in code, not prompt text: a per-turn spawn
cap counted in its own state channel (``subagent_count``, separate
from S4's ``delegation_count``), a wall-clock timeout, per-sub-agent
token/cost budgets read from the UsageLedger, a read-only-by-default
tool surface that can only grow within the parent's own surface, and
a hard depth cap — supervisor -> specialist -> sub-agent — enforced
by construction (a child run's tool surface has no spawn tool).
Child tool calls traverse the SAME policy -> approval -> sandbox
pipeline: an approval pause inside a child bridges through the
parent graph's interrupt and resumes into the child.

These tests drive the real graph through the executor with scripted
providers (the E1-11 / S12 harness): the provider dispatches on
node_name, and distinguishes a parent specialist call from a child
call by the tools it is offered (the parent is offered
``spawn_subagent``; a child never is).
"""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine import task_ledger
from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.cost.ledger import UsageLedger, record_provider_call
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph import subagents as subagents_module
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes.researcher import RESEARCHER_SPEC
from aether_engine.langgraph.nodes.runner import run_specialist
from aether_engine.langgraph.subagents import (
    SPAWN_TOOL_NAME,
    resolve_child_surface,
    subagent_settings,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

CHILD_ANSWER = "The capital of France is Paris."
PARENT_FINAL = "Done — the answer is Paris."


def _tool_names(tools):
    return [
        (t.get("function") or {}).get("name")
        for t in (tools or [])
    ]


def _spawn_call(role="researcher", task="find the capital of France", tools=None):
    args = {"role": role, "task": task}
    if tools is not None:
        args["tools"] = tools
    return ToolCall(name=SPAWN_TOOL_NAME, args=args, call_id="call-spawn")


class SpawnFlowProvider:
    """Supervisor -> coder; coder spawns a researcher sub-agent, then
    answers. Researcher calls are the child's (coder is the parent)."""

    def __init__(self):
        self.calls = []  # (node_name, tool_names, messages)
        self._supervisor = 0
        self._coder = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append((node_name, _tool_names(tools), messages))
        if node_name == "researcher":
            yield StreamChunk(text=CHILD_ANSWER, finish_reason="stop")
            return
        if node_name == "coder":
            self._coder += 1
            if self._coder == 1:
                yield StreamChunk(
                    tool_calls=[_spawn_call()], finish_reason="tool_calls")
            else:
                yield StreamChunk(text=PARENT_FINAL, finish_reason="stop")
            return
        self._supervisor += 1
        text = (
            '{"next": "coder", "reason": "needs work", "briefing": ""}'
            if self._supervisor == 1
            else '{"next": "END", "reason": "done", "briefing": ""}')
        yield StreamChunk(text=text, finish_reason="stop")

    def calls_for(self, node):
        return [c for c in self.calls if c[0] == node]


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        subagents_module, "SUBAGENT_CHECKPOINT_DB",
        tmp_path / "subagent_checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "s13/plan.txt")
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "s13/code.txt")
    monkeypatch.setattr(researcher_module, "save_artifact", lambda *a, **k: "s13/research.txt")
    task_ledger.clear_tap()
    return tmp_path


async def _run(provider, session_id, user_config=None, registry=None,
               prompt="please write some code"):
    return [ev async for ev in executor_module.run_langgraph_task(
        prompt,
        provider,
        tools=registry or ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config or UserConfig(),
    )]


def _subagent_activities(events):
    return [
        ev.payload for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and str((ev.payload or {}).get("action", "")).startswith("subagent_")
    ]


class _EmptyBackend:
    def list_episodes(self, limit=50):
        return []


# ── Config + validation ─────────────────────────────────────────────

def test_subagent_config_defaults_and_round_trip():
    cfg = UserConfig()
    assert cfg.subagents_enabled is True
    assert cfg.subagents_max_per_turn == 4
    assert cfg.subagent_timeout_s == 120
    assert cfg.subagent_token_budget == 20000
    assert cfg.subagent_cost_budget_usd == 0.0

    custom = UserConfig(
        subagents_enabled=False, subagents_max_per_turn=2,
        subagent_timeout_s=30, subagent_token_budget=500,
        subagent_cost_budget_usd=0.25)
    restored = UserConfig.from_dict(custom.to_dict())
    assert restored.subagents_enabled is False
    assert restored.subagents_max_per_turn == 2
    assert restored.subagent_timeout_s == 30
    assert restored.subagent_token_budget == 500
    assert restored.subagent_cost_budget_usd == 0.25

    corrupt = UserConfig.from_dict({
        "subagents_max_per_turn": "soon", "subagent_timeout_s": None})
    assert corrupt.subagents_max_per_turn == 4
    assert corrupt.subagent_timeout_s == 120

    settings = subagent_settings(custom)
    assert settings.enabled is False and settings.max_per_turn == 2
    assert subagent_settings(None).max_per_turn == 4


def test_subagent_config_updates_are_strictly_validated():
    cleaned = validate_handed_off_updates({
        "subagents_enabled": False,
        "subagents_max_per_turn": 3,
        "subagent_timeout_s": 45,
        "subagent_token_budget": 0,
        "subagent_cost_budget_usd": 0.5,
    })
    assert cleaned["subagents_enabled"] is False
    assert cleaned["subagents_max_per_turn"] == 3
    assert cleaned["subagent_token_budget"] == 0  # 0 disables the budget
    assert cleaned["subagent_cost_budget_usd"] == 0.5

    with pytest.raises(ValueError):
        validate_handed_off_updates({"subagents_enabled": "yes"})
    with pytest.raises(ValueError):
        validate_handed_off_updates({"subagents_max_per_turn": -1})
    with pytest.raises(ValueError):
        validate_handed_off_updates({"subagent_timeout_s": 0})
    with pytest.raises(ValueError):
        validate_handed_off_updates({"subagent_cost_budget_usd": -0.5})


# ── Tool surface resolution ─────────────────────────────────────────

def _registry_with(*specs):
    registry = ToolRegistry()
    for name, risk in specs:
        registry.register(Tool(
            name=name, description=f"{name} tool",
            execute_fn=lambda **kw: "ok", risk=risk))
    return registry


def test_surface_defaults_to_read_only_within_the_parents_surface():
    registry = _registry_with(
        ("read_file", ToolRisk.READ_ONLY), ("write_file", ToolRisk.WRITE))
    surface, denial = resolve_child_surface(registry, {}, "coder", None)
    assert denial is None
    assert surface == ["read_file"]


def test_surface_grants_only_what_the_parent_has():
    registry = _registry_with(
        ("read_file", ToolRisk.READ_ONLY), ("write_file", ToolRisk.WRITE))
    # The parent is allow-listed to read_file only: write_file is not
    # the parent's to grant, so asking for it is a denial (never a
    # silent clamp).
    configurable = {"agent_tools": {"coder": ["read_file"]}}
    surface, denial = resolve_child_surface(
        registry, configurable, "coder", ["write_file"])
    assert surface == []
    assert "write_file" in denial and "not available to the coder" in denial
    # Granted when the parent has it.
    surface, denial = resolve_child_surface(
        registry, {}, "coder", ["write_file"])
    assert denial is None
    assert surface == ["read_file", "write_file"]
    # The spawn tool itself is never grantable.
    surface, denial = resolve_child_surface(
        registry, {}, "coder", [SPAWN_TOOL_NAME])
    assert denial is None and SPAWN_TOOL_NAME not in surface


async def test_spawn_is_denied_in_plan_mode_at_the_seam():
    # Plan mode is plan-only (the supervisor spawn policy's stance):
    # the tool is not offered there, and the seam backstops a forged
    # or stale pending call with a surfaced denial.
    registry = _registry_with(("read_file", ToolRisk.READ_ONLY))
    state = {
        "active_specialist": "coder", "task_id": "s13-plan",
        "session_id": "s13-plan-session", "subagent_count": 0,
        "plan_mode": True, "blackboard": {},
    }
    config = {"configurable": {"tool_registry": registry,
                               "user_config": UserConfig()}}
    update = await subagents_module.execute_spawn(
        state, config,
        {"name": SPAWN_TOOL_NAME,
         "args": {"role": "researcher", "task": "look something up"},
         "call_id": "call-plan"})
    (tool_msg,) = update["messages"]
    assert "plan mode" in tool_msg["content"]
    assert "Sub-agent spawn denied" in tool_msg["content"]
    assert "subagent_count" not in update  # a denial never counts


# ── Depth enforcement + toggle (runner level) ───────────────────────

class _CaptureProvider:
    """Records the tools + system prompt of the one call it serves."""

    def __init__(self):
        self.tools = None
        self.system_prompt = None

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.tools = tools
        self.system_prompt = messages[0]["content"] if messages else ""
        yield StreamChunk(text="answer", finish_reason="stop")


def _runner_state():
    return {
        "original_prompt": "Do the task",
        "messages": [{"role": "user", "content": "Do the task"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": "researcher",
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "s13-runner",
        "session_id": "s13-runner-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }


async def test_spawn_tool_offered_to_specialists_but_never_to_a_child():
    registry = _registry_with(("read_file", ToolRisk.READ_ONLY))
    provider = _CaptureProvider()
    await run_specialist(
        RESEARCHER_SPEC, _runner_state(),
        {"configurable": {"provider": provider, "tool_registry": registry,
                          "user_config": UserConfig()}},
        save_artifact=lambda *a, **k: "/tmp/x.txt")
    assert SPAWN_TOOL_NAME in _tool_names(provider.tools)

    child_provider = _CaptureProvider()
    await run_specialist(
        RESEARCHER_SPEC, _runner_state(),
        {"configurable": {"provider": child_provider, "tool_registry": registry,
                          "user_config": UserConfig(), "is_subagent_run": True,
                          "agent_tools": {"researcher": ["read_file"]}}},
        save_artifact=lambda *a, **k: "/tmp/x.txt")
    # Depth by construction: the child's surface has no spawn tool —
    # and nothing beyond its granted surface at all.
    assert _tool_names(child_provider.tools) == ["read_file"]


async def test_toggle_off_removes_the_tool_and_prompts_are_unchanged():
    registry = _registry_with(("read_file", ToolRisk.READ_ONLY))
    on = _CaptureProvider()
    await run_specialist(
        RESEARCHER_SPEC, _runner_state(),
        {"configurable": {"provider": on, "tool_registry": registry,
                          "user_config": UserConfig()}},
        save_artifact=lambda *a, **k: "/tmp/x.txt")
    off = _CaptureProvider()
    await run_specialist(
        RESEARCHER_SPEC, _runner_state(),
        {"configurable": {"provider": off, "tool_registry": registry,
                          "user_config": UserConfig(subagents_enabled=False)}},
        save_artifact=lambda *a, **k: "/tmp/x.txt")
    assert SPAWN_TOOL_NAME in _tool_names(on.tools)
    assert SPAWN_TOOL_NAME not in _tool_names(off.tools)
    # The contract rides the tool description, never the prompt: with
    # the toggle off the system prompt is byte-identical.
    assert off.system_prompt == on.system_prompt


# ── Happy path through the whole graph ──────────────────────────────

@pytest.mark.asyncio
async def test_spawn_happy_path_summary_blackboard_and_ledger(graph_env):
    tmp_path = graph_env
    provider = SpawnFlowProvider()
    events = await _run(provider, "s13-happy")
    types = [ev.type for ev in events]
    assert EventType.TASK_COMPLETED in types
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert PARENT_FINAL in completed[-1].payload.get("response", "")

    # Structured activity: spawn then completion, with parent linkage.
    activities = _subagent_activities(events)
    assert [a["action"] for a in activities] == [
        "subagent_spawn", "subagent_completed"]
    spawn_ev, done_ev = activities
    assert spawn_ev["from_node"] == "coder"
    assert spawn_ev["to_node"] == "researcher"
    assert spawn_ev["parent_agent"] == "coder"
    assert spawn_ev["subagent_seq"] == 1
    assert done_ev["output_payload"]["status"] == "completed"
    assert done_ev["output_payload"]["tools_used"] == []

    # The parent specialist's next call saw the summary as its tool
    # result AND the digest on the blackboard briefing in its prompt.
    coder_calls = provider.calls_for("coder")
    assert len(coder_calls) == 2
    second_prompt = coder_calls[1][2][0]["content"]
    assert "[Sub-agent researcher #1]" in second_prompt
    assert "Paris" in second_prompt
    tool_msgs = [
        m for m in coder_calls[1][2]
        if isinstance(m, dict) and m.get("role") == "tool"
        and m.get("name") == SPAWN_TOOL_NAME]
    assert tool_msgs and "completed" in tool_msgs[0]["content"]
    assert CHILD_ANSWER in tool_msgs[0]["content"]

    # The supervisor's next routing prompt shows the digest via the
    # blackboard briefing (the delegation-product surface).
    supervisor_calls = provider.calls_for("supervisor")
    assert len(supervisor_calls) >= 2
    assert "[Sub-agent researcher #1]" in supervisor_calls[1][2][0]["content"]

    # The S3 ledger attributes the child work to the parent task.
    tap = task_ledger.tap_snapshot("task-s13-happy")
    assert tap is not None
    assert [r["action"] for r in tap["subagents"]] == [
        "subagent_spawn", "subagent_completed"]
    assert tap["subagents"][1]["parent_agent"] == "coder"
    assert tap["subagents"][1]["role"] == "researcher"
    summary = await task_ledger.task_summary(
        "task-s13-happy", session_id="s13-happy", backend=_EmptyBackend(),
        ledger=UsageLedger(tmp_path / "usage.db"),
        checkpoint_db=tmp_path / "checkpoints.db")
    assert summary["found"] is True
    assert summary["subagents"], "the summary must carry the sub-agent records"
    briefing = task_ledger.task_briefing(summary)
    assert "Sub-agents:" in briefing
    assert "researcher #1 (completed) by coder" in briefing


# ── Denials: toggle off + per-turn cap ──────────────────────────────

class DoubleSpawnProvider(SpawnFlowProvider):
    """Coder tries to spawn twice, then answers."""

    def __init__(self):
        super().__init__()
        self._coder = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "coder":
            self.calls.append((node_name, _tool_names(tools), messages))
            self._coder += 1
            if self._coder <= 2:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=SPAWN_TOOL_NAME,
                        args={"role": "researcher",
                              "task": "find the capital of France"},
                        call_id=f"call-spawn-{self._coder}")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text=PARENT_FINAL, finish_reason="stop")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_per_turn_cap_denies_the_second_spawn_at_the_bound(graph_env):
    provider = DoubleSpawnProvider()
    events = await _run(
        provider, "s13-cap", user_config=UserConfig(subagents_max_per_turn=1))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    # Exactly ONE child run happened.
    assert len(provider.calls_for("researcher")) == 1
    activities = _subagent_activities(events)
    assert [a["action"] for a in activities] == [
        "subagent_spawn", "subagent_completed", "subagent_denied"]
    assert "cap for one turn (1)" in activities[-1]["reason"]
    # The denial reached the parent as its tool result (never silent).
    coder_calls = provider.calls_for("coder")
    assert len(coder_calls) == 3
    tool_msgs = [
        m for m in coder_calls[2][2]
        if isinstance(m, dict) and m.get("role") == "tool"
        and m.get("name") == SPAWN_TOOL_NAME]
    assert any("Sub-agent spawn denied" in (m.get("content") or "")
               for m in tool_msgs)


@pytest.mark.asyncio
async def test_toggle_off_denies_a_spawn_attempt_with_the_reason(graph_env):
    provider = SpawnFlowProvider()
    events = await _run(
        provider, "s13-off", user_config=UserConfig(subagents_enabled=False))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    assert provider.calls_for("researcher") == []  # no child ever ran
    activities = _subagent_activities(events)
    assert [a["action"] for a in activities] == ["subagent_denied"]
    assert "disabled" in activities[0]["reason"]


# ── Timeout ─────────────────────────────────────────────────────────

class SlowChildProvider(SpawnFlowProvider):
    """The child never answers inside the budget."""

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "researcher":
            self.calls.append((node_name, _tool_names(tools), messages))
            await asyncio.sleep(60)
            yield StreamChunk(text="too late", finish_reason="stop")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_timeout_returns_a_failure_summary_and_the_parent_completes(graph_env):
    provider = SlowChildProvider()
    started = time.time()
    events = await _run(
        provider, "s13-timeout", user_config=UserConfig(subagent_timeout_s=1))
    elapsed = time.time() - started
    assert elapsed < 20, "the timeout must actually stop the child"
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    activities = _subagent_activities(events)
    assert [a["action"] for a in activities] == [
        "subagent_spawn", "subagent_failed"]
    assert activities[-1]["output_payload"]["status"] == "timed_out"
    coder_calls = provider.calls_for("coder")
    tool_msgs = [
        m for m in coder_calls[1][2]
        if isinstance(m, dict) and m.get("role") == "tool"
        and m.get("name") == SPAWN_TOOL_NAME]
    assert tool_msgs and "timed out" in tool_msgs[0]["content"]


# ── Token budget (UsageLedger enforcement) ──────────────────────────

class UsageRecordingChildProvider(SpawnFlowProvider):
    """Child round 1 records 25,000 tokens of usage in the ledger
    (a usage-reporting provider), then calls a read-only tool. If
    the budget gate works, there is no round 2 model call."""

    def __init__(self):
        super().__init__()
        self.child_model_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "researcher":
            self.calls.append((node_name, _tool_names(tools), messages))
            self.child_model_calls += 1
            record_provider_call(
                model_name="fake-model", provider_name="fake",
                input_tokens=15000, output_tokens=10000,
                agent_name="researcher", messages=[], output_text="x")
            yield StreamChunk(
                tool_calls=[ToolCall(name="lookup_note", args={},
                                     call_id="call-lookup")],
                finish_reason="tool_calls")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_token_budget_stops_the_child_between_model_calls(
        graph_env, monkeypatch):
    tmp_path = graph_env
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        "aether_engine.cost.ledger.get_usage_ledger", lambda: ledger)
    monkeypatch.setattr(subagents_module, "get_usage_ledger", lambda: ledger)
    executed = []
    registry = _registry_with(("lookup_note", ToolRisk.READ_ONLY))
    registry.get_tool("lookup_note").execute_fn = (
        lambda **kw: executed.append(kw) or "note-result")

    provider = UsageRecordingChildProvider()
    events = await _run(
        provider, "s13-budget", registry=registry,
        user_config=UserConfig(subagent_token_budget=20000))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    # The first round ran (and its tool executed); the budget gate
    # stopped the child before a second model call.
    assert executed, "the child's first tool call should have executed"
    assert provider.child_model_calls == 1
    activities = _subagent_activities(events)
    assert activities[-1]["output_payload"]["status"] == "budget_exhausted"
    assert activities[-1]["output_payload"]["tokens_used"] >= 25000
    coder_calls = provider.calls_for("coder")
    tool_msgs = [
        m for m in coder_calls[1][2]
        if isinstance(m, dict) and m.get("role") == "tool"
        and m.get("name") == SPAWN_TOOL_NAME]
    assert tool_msgs and "token budget" in tool_msgs[0]["content"]


# ── Approvals bridge through the parent graph ───────────────────────

WRITE_ARGS = {"path": "out.txt", "content": "hello"}


class ApprovalChildProvider(SpawnFlowProvider):
    """Coder spawns a researcher granted a WRITE tool; the child
    calls it (policy: Require Approval), then finishes."""

    def __init__(self):
        super().__init__()
        self._child = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "researcher":
            self.calls.append((node_name, _tool_names(tools), messages))
            self._child += 1
            if self._child == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name="write_thing", args=dict(WRITE_ARGS),
                                         call_id="call-write")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text="Child finished.", finish_reason="stop")
            return
        if node_name == "coder":
            self.calls.append((node_name, _tool_names(tools), messages))
            self._coder += 1
            if self._coder == 1:
                yield StreamChunk(
                    tool_calls=[_spawn_call(task="write the file",
                                            tools=["write_thing"])],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text=PARENT_FINAL, finish_reason="stop")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


def _write_registry(executed):
    registry = ToolRegistry()
    registry.register(Tool(
        name="write_thing", description="Writes a thing",
        execute_fn=lambda **kw: executed.append(kw) or "written",
        risk=ToolRisk.WRITE))
    return registry


@pytest.mark.asyncio
async def test_approval_inside_a_subagent_pauses_the_parent_and_resumes(
        graph_env, monkeypatch):
    executed = []
    registry = _write_registry(executed)
    # Non-READ_ONLY tools execute in the worker sandbox (ADR 0010),
    # whose Windows job objects cannot run on this machine — the
    # suite's established fake worker (cf. test_epic8_n05) stands in
    # for it; policy and approval, the layers under test, are real.
    from aether_engine.workers import sandbox

    async def _fake_worker(tool_name, args, task_class="standard",
                           audit_logger=None, task_classes=None):
        executed.append(dict(args))
        return "written"

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _fake_worker)
    provider = ApprovalChildProvider()
    user_config = UserConfig(tool_policies={"write_thing": "Require Approval"})
    events = await _run(provider, "s13-approve", user_config=user_config,
                        registry=registry)
    types = [ev.type for ev in events]
    # The child's ask-policy call paused the WHOLE task through the
    # parent graph's own approval channel — and nothing executed.
    assert EventType.TOOL_APPROVAL_REQUEST in types
    approval = [ev for ev in events if ev.type == EventType.TOOL_APPROVAL_REQUEST]
    assert approval[-1].payload.get("tool_name") == "write_thing"
    assert EventType.TASK_INTERRUPTED in types
    assert executed == []

    resume_events = [ev async for ev in executor_module.resume_langgraph_task(
        "s13-approve", True, provider, tools=registry)]
    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [WRITE_ARGS]
    activities = _subagent_activities(resume_events)
    assert activities and activities[-1]["action"] == "subagent_completed"


@pytest.mark.asyncio
async def test_denied_approval_inside_a_subagent_executes_nothing(graph_env):
    executed = []
    registry = _write_registry(executed)
    provider = ApprovalChildProvider()
    user_config = UserConfig(tool_policies={"write_thing": "Require Approval"})
    events = await _run(provider, "s13-deny", user_config=user_config,
                        registry=registry)
    assert EventType.TASK_INTERRUPTED in [ev.type for ev in events]

    resume_events = [ev async for ev in executor_module.resume_langgraph_task(
        "s13-deny", False, provider, tools=registry)]
    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [], "a denied approval must execute nothing, at any depth"
    activities = _subagent_activities(resume_events)
    assert activities and activities[-1]["action"] == "subagent_completed"


# ── Retry once on child error ───────────────────────────────────────

class FlakyChildProvider(SpawnFlowProvider):
    """The child's first run raises; its second answers."""

    def __init__(self):
        super().__init__()
        self.child_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "researcher":
            self.calls.append((node_name, _tool_names(tools), messages))
            self.child_calls += 1
            if self.child_calls == 1:
                raise RuntimeError("provider exploded")
            yield StreamChunk(text=CHILD_ANSWER, finish_reason="stop")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_a_failed_child_run_is_retried_once_within_budget(graph_env):
    provider = FlakyChildProvider()
    events = await _run(provider, "s13-retry")
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    assert provider.child_calls == 2
    activities = _subagent_activities(events)
    assert activities[-1]["action"] == "subagent_completed"
    assert activities[-1]["output_payload"]["attempts"] == 2


# ── Parent cancellation leaves no orphans ───────────────────────────

class BlockingChildProvider(SpawnFlowProvider):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "researcher":
            self.calls.append((node_name, _tool_names(tools), messages))
            self.started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            yield StreamChunk(text="unreachable", finish_reason="stop")
            return
        async for chunk in SpawnFlowProvider.call_stream(
                self, messages, tools=tools, node_name=node_name, **kwargs):
            yield chunk


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_the_child(graph_env):
    provider = BlockingChildProvider()

    async def _consume():
        return [ev async for ev in executor_module.run_langgraph_task(
            "please write some code", provider, tools=ToolRegistry(),
            task_id="task-s13-cancel", request_id="req-s13-cancel",
            session_id="s13-cancel", user_config=UserConfig())]

    consume_task = asyncio.create_task(_consume())
    await asyncio.wait_for(provider.started.wait(), timeout=15)
    consume_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consume_task
    # The running child was cancelled with the parent — no orphan
    # keeps working (or writing) after the turn ends.
    assert provider.cancelled
    await asyncio.sleep(0.2)
    tap = task_ledger.tap_snapshot("task-s13-cancel")
    assert tap is not None
    assert [r["action"] for r in tap["subagents"]] == ["subagent_spawn"]


# ── Child config derivation ─────────────────────────────────────────

def test_child_config_drops_prewalk_schemas_and_advisor_only():
    cfg = UserConfig(
        prewalk_agents=["coder"],
        agent_output_schemas={"coder": {"type": "object"}},
        auxiliary_models={"advisor": "x:::y", "vision": "x:::z"},
        tool_policies={"write_thing": "Require Approval"})
    child = subagents_module._derived_user_config(cfg)
    assert child.prewalk_agents == []
    assert child.agent_output_schemas == {}
    assert "advisor" not in child.auxiliary_models
    assert child.auxiliary_models["vision"] == "x:::z"
    # The policy pipeline inputs are the parent's own, unchanged.
    assert child.tool_policies == {"write_thing": "Require Approval"}
    assert subagents_module._derived_user_config(None) is None
