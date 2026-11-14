"""Master build S4: single delegation counter + delegation read-back.

Before S4 the delegation cap was derived twice, in two units that
could not see each other (audit G10): ``_spawn_policy_denial``
counted handoff tool calls in the message record, and
``_chain_end_reason`` counted specialist *answers*. Handoff messages
are consumed by the supervisor framework and never re-enter the
model's view, so neither derivation was authoritative.

S4 replaces both with one counter in state:
``AetherState["delegation_count"]``, incremented in exactly one
place (``supervisor_post_model_hook``, when a handoff has passed
the spawn policy and is about to execute), seeded to 0 by the
executor for each new run (the cap is per user turn), and
checkpointed so it survives the approval pause. The spawn-policy
surfaces are unchanged: allow-list / plan-mode / cap denials read
the same, a capped post-specialist chain still ends with the latest
answer standing, and cap 0 on the first hop still denies.

The read-back half: ``delegation_briefing()`` renders the delegation
log in the ``blackboard_briefing()`` pattern (bounded, empty when
nothing was delegated) and the routing prompt injects it, so the
supervisor can see what it already delegated (audit G7).
"""
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.state import delegation_briefing
from aether_engine.langgraph.supervisor_model import (
    _delegation_count,
    _spawn_policy_denial,
    supervisor_post_model_hook,
    supervisor_routing_prompt,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.task_ledger import _state_slice
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

PLAN_TEXT = "Plan: 1) write the code, 2) ship it."
CODE_TEXT = "Implementation complete: the code is written."
FINAL_TEXT = "Specialist final answer."
TOOL_NAME = "s4_probe"
TOOL_ARGS = {"q": "x"}
TOOL_RESULT = "probe-result-s4"


def _config(user_config):
    return {"configurable": {"user_config": user_config}}


def _handoff_message(target, content="routing"):
    return AIMessage(
        content=content,
        tool_calls=[{"name": f"transfer_to_{target}", "args": {}, "id": f"call-{target}"}],
    )


# ── The counter derivation ───────────────────────────────────────────

def test_counter_reads_the_state_field_when_present():
    state = {"delegation_count": 3, "messages": [], "delegation_log": []}
    assert _delegation_count(state) == 3
    assert _delegation_count({"delegation_count": 0}) == 0


def test_counter_falls_back_to_the_log_for_pre_s4_states():
    """A checkpoint written before S4 has no counter; the log is the
    record of the same events (denied attempts and END / memory-step
    entries are not delegations and never count)."""
    state = {"delegation_log": [
        {"decision": {"next": "planner", "reason": "plan"}},
        {"decision": {"next": "coder", "reason": "no", "denied": True}},
        {"decision": {"next": "END", "reason": "done"}},
        {"decision": {"next": "supervisor", "reason": "memory step"}},
        {"decision": {"next": "coder", "reason": "build"}},
    ]}
    assert _delegation_count(state) == 2


def test_counter_falls_back_to_messages_excluding_the_pending_handoff():
    prior = _handoff_message("planner")
    pending = _handoff_message("coder")
    state = {"messages": [HumanMessage(content="multi"), prior,
                          AIMessage(content="planned"), pending]}
    assert _delegation_count(state) == 1


# ── Spawn policy judged on the counter ───────────────────────────────

def test_cap_denial_triggers_at_the_counter_cap():
    """Counter at 1 (one delegation already executed), pending is next:
    cap 2 allows it, cap 1 denies it — the same points the old
    message-derived count produced for this scenario."""
    state = {"messages": [HumanMessage(content="multi-step"),
                          _handoff_message("coder")],
             "delegation_count": 1}
    assert _spawn_policy_denial(
        state, "coder", _config(UserConfig(supervisor_delegation_cap=2))) is None
    denial = _spawn_policy_denial(
        state, "coder", _config(UserConfig(supervisor_delegation_cap=1)))
    assert denial is not None
    assert "delegation cap" in denial["delegation_log"][0]["decision"]["reason"]


def test_cap_zero_denies_the_first_hop():
    state = {"messages": [HumanMessage(content="hi"), _handoff_message("planner")],
             "delegation_count": 0}
    denial = _spawn_policy_denial(
        state, "planner", _config(UserConfig(supervisor_delegation_cap=0)))
    assert denial is not None
    assert "delegation cap" in denial["delegation_log"][0]["decision"]["reason"]


def test_counter_wins_over_the_message_record():
    """The disagreement the audit flagged, pinned: messages show a
    prior handoff but the counter says none executed this turn (the
    handoff was the pending shape from a rebuilt state) — the counter
    is authoritative and the delegation is allowed at cap 1."""
    state = {"messages": [HumanMessage(content="multi"),
                          _handoff_message("planner"),
                          AIMessage(content="planned"),
                          _handoff_message("coder")],
             "delegation_count": 0}
    assert _spawn_policy_denial(
        state, "coder", _config(UserConfig(supervisor_delegation_cap=1))) is None


# ── The hook: the single increment ───────────────────────────────────

def test_hook_increments_only_on_an_executing_handoff():
    state = {"messages": [HumanMessage(content="x"), _handoff_message("planner")],
             "delegation_count": 2}
    update = supervisor_post_model_hook(state)
    assert update["delegation_count"] == 3
    assert update["active_specialist"] == "planner"


def test_hook_denial_does_not_increment():
    state = {"messages": [HumanMessage(content="x"), _handoff_message("coder")],
             "delegation_count": 2, "plan_mode": True}
    update = supervisor_post_model_hook(state)
    assert update["delegation_log"][0]["decision"]["denied"] is True
    assert "delegation_count" not in update


def test_hook_end_decision_does_not_increment():
    decision = AIMessage(
        content="all done",
        additional_kwargs={"aether_decision": {
            "next": "END", "reason": "finished", "briefing": ""}})
    update = supervisor_post_model_hook(
        {"messages": [HumanMessage(content="x"), decision], "delegation_count": 1})
    assert update["delegation_log"][0]["decision"]["next"] == "END"
    assert "delegation_count" not in update


def test_hook_cap_with_an_answer_standing_ends_the_chain_not_denies():
    """The pre-S4 cap surface at the model seam, preserved at the
    hook: counter at the (default) cap and a specialist answer in
    view — the pending handoff is replaced by an end-guard message,
    the log records an END decision, and no denial is written."""
    state = {"messages": [HumanMessage(content="work"),
                          AIMessage(content=FINAL_TEXT),
                          _handoff_message("planner")],
             "delegation_count": 8}
    update = supervisor_post_model_hook(state)
    (stripped,) = update["messages"]
    assert not stripped.tool_calls
    assert stripped.content == ""
    assert (stripped.additional_kwargs or {}).get("aether_end_guard") is True
    entry = update["delegation_log"][0]["decision"]
    assert entry["next"] == "END"
    assert not entry.get("denied")
    assert "delegation_count" not in update
    assert update["active_specialist"] is None


# ── delegation_briefing ──────────────────────────────────────────────

def _log(*decisions):
    return [{"agent": "supervisor", "timestamp": 0.0, "decision": d}
            for d in decisions]


def test_briefing_renders_who_why_and_denied():
    state = {"delegation_log": _log(
        {"next": "planner", "reason": "plan it first"},
        {"next": "coder", "reason": "over the cap", "denied": True},
    )}
    text = delegation_briefing(state)
    assert "planner" in text and "plan it first" in text
    assert "coder (denied)" in text


def test_briefing_excludes_end_and_memory_steps():
    state = {"delegation_log": _log(
        {"next": "END", "reason": "finished"},
        {"next": "supervisor", "reason": "memory step"},
    )}
    assert delegation_briefing(state) == ""


def test_briefing_empty_when_nothing_delegated():
    assert delegation_briefing({}) == ""
    assert delegation_briefing({"delegation_log": []}) == ""


def test_briefing_is_capped_and_shows_only_recent_entries():
    state = {"delegation_log": _log(*[
        {"next": f"agent{i}", "reason": "r" * 200} for i in range(12)])}
    text = delegation_briefing(state)
    assert len(text) <= 1200
    assert "latest 8 of 12" in text
    assert "agent11" in text and "agent0" not in text
    assert len(delegation_briefing(state, max_chars=50)) <= 50


# ── Routing prompt read-back ─────────────────────────────────────────

def test_routing_prompt_includes_briefing_when_delegations_exist():
    state = {
        "original_prompt": "do the work", "plan": "No plan yet.",
        "messages": [HumanMessage(content="do the work")],
        "delegation_log": _log({"next": "planner", "reason": "plan it first"}),
    }
    prompt = supervisor_routing_prompt(state)
    assert "Delegation history for this task" in prompt[0].content
    assert "planner" in prompt[0].content


def test_routing_prompt_unchanged_when_no_delegations():
    state = {
        "original_prompt": "do the work", "plan": "No plan yet.",
        "messages": [HumanMessage(content="do the work")],
        "delegation_log": [],
    }
    prompt = supervisor_routing_prompt(state)
    assert "Delegation history for this task" not in prompt[0].content


# ── Ledger consistency (additive) ────────────────────────────────────

def test_ledger_slice_carries_the_counter_additively():
    slice_ = _state_slice({"delegation_log": [], "delegation_count": 2})
    assert slice_["delegation_count"] == 2
    assert slice_["delegations"] == []
    assert _state_slice({"delegation_log": []})["delegation_count"] is None


# ── End-to-end through the graph ─────────────────────────────────────

class ChainProvider:
    """Supervisor script: planner -> coder -> END (JSON decisions)."""

    def __init__(self):
        self.calls = []
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "planner":
            yield StreamChunk(text=PLAN_TEXT, finish_reason="stop")
            return
        if node_name == "coder":
            yield StreamChunk(text=CODE_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "plan it first", "briefing": ""}'
        elif self._supervisor_calls == 2:
            text = '{"next": "coder", "reason": "implement the plan", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "chain finished", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")

    def nodes(self):
        return [c["node"] for c in self.calls]


class SingleHopProvider:
    """Supervisor script: planner -> END, per run."""

    def __init__(self):
        self.calls = []
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append(node_name)
        if node_name == "planner":
            yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "handle it", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "done", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")


class ApprovalProvider:
    """Supervisor: planner -> END. Planner: probe tool, then final."""

    def __init__(self):
        self._supervisor_calls = 0
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-s4-1")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "probe it", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "done", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "s4/plan.txt")
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "s4/code.txt")
    return tmp_path


async def _run(provider, session_id, user_config=None, task_id=None, registry=None):
    return [ev async for ev in executor_module.run_langgraph_task(
        "do the work",
        provider,
        tools=registry or ToolRegistry(),
        task_id=task_id or f"task-{session_id}",
        request_id=f"req-{task_id or session_id}",
        session_id=session_id,
        user_config=user_config or UserConfig(),
    )]


async def _checkpoint_values(db_path, session_id):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from aether_engine.langgraph.graph import compile_graph

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = compile_graph(checkpointer=saver)
        state = await graph.aget_state({"configurable": {"thread_id": session_id}})
    return state.values


@pytest.mark.asyncio
async def test_counter_counts_a_multi_delegation_turn(graph_env):
    provider = ChainProvider()
    events = await _run(provider, "s4-chain")
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    values = await _checkpoint_values(graph_env / "checkpoints.db", "s4-chain")
    assert values.get("delegation_count") == 2
    delegations = [
        (e.get("decision") or {}).get("next")
        for e in values.get("delegation_log") or []
        if (e.get("decision") or {}).get("next") not in (None, "END")]
    assert delegations == ["planner", "coder"]


@pytest.mark.asyncio
async def test_routing_prompt_reads_back_delegations_during_the_run(graph_env):
    provider = ChainProvider()
    await _run(provider, "s4-readback")
    supervisor_calls = [c for c in provider.calls if c["node"] == "supervisor"]
    assert len(supervisor_calls) == 3
    systems = [
        "\n".join(m["content"] for m in c["messages"] if m.get("role") == "system")
        for c in supervisor_calls]
    assert "Delegation history for this task" not in systems[0]
    assert "Delegation history for this task" in systems[1]
    assert "planner" in systems[1]
    assert "Delegation history for this task" in systems[2]
    assert "coder" in systems[2]


@pytest.mark.asyncio
async def test_cap_one_ends_chain_with_the_answer_standing(graph_env):
    """Cap surface preserved: at cap 1 the second delegation never
    runs, the task completes with the planner's answer as its
    response, and no spawn-policy denial text appears."""
    provider = ChainProvider()
    events = await _run(provider, "s4-cap1",
                        user_config=UserConfig(supervisor_delegation_cap=1))
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed, "the capped chain must still complete"
    response = completed[-1].payload.get("response", "")
    assert PLAN_TEXT in response
    assert "spawn policy" not in response
    assert "coder" not in provider.nodes()
    values = await _checkpoint_values(graph_env / "checkpoints.db", "s4-cap1")
    assert values.get("delegation_count") == 1


@pytest.mark.asyncio
async def test_cap_zero_denies_the_first_hop_end_to_end(graph_env):
    provider = ChainProvider()
    events = await _run(provider, "s4-cap0",
                        user_config=UserConfig(supervisor_delegation_cap=0))
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed
    response = completed[-1].payload.get("response", "")
    assert "spawn policy" in response and "delegation cap" in response
    assert "planner" not in provider.nodes()
    values = await _checkpoint_values(graph_env / "checkpoints.db", "s4-cap0")
    assert values.get("delegation_count") == 0


@pytest.mark.asyncio
async def test_counter_resets_on_a_new_user_turn(graph_env):
    """The cap is per turn: a second run on the same thread/session
    delegates again at cap 1 — the counter was re-seeded, not carried
    over from the previous turn."""
    cfg = UserConfig(supervisor_delegation_cap=1)
    first = await _run(SingleHopProvider(), "s4-turns", user_config=cfg,
                       task_id="task-s4-turn-1")
    assert EventType.TASK_COMPLETED in [ev.type for ev in first]
    second_provider = SingleHopProvider()
    second = await _run(second_provider, "s4-turns", user_config=cfg,
                        task_id="task-s4-turn-2")
    completed = [ev for ev in second if ev.type == EventType.TASK_COMPLETED]
    assert completed, "the second turn must complete, not cap-deny"
    assert "spawn policy" not in completed[-1].payload.get("response", "")
    assert "planner" in second_provider.calls
    values = await _checkpoint_values(graph_env / "checkpoints.db", "s4-turns")
    assert values.get("delegation_count") == 1


@pytest.mark.asyncio
async def test_counter_survives_the_approval_pause(graph_env):
    """One delegation, paused mid-specialist for a tool approval:
    the counter already reads 1 at the pause, and after resume it is
    still 1 — the approved delegation is never counted twice."""
    executed = []
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return TOOL_RESULT

    registry.register(Tool(
        name=TOOL_NAME, description="S4 approval probe tool",
        execute_fn=_fn, risk=ToolRisk.READ_ONLY))
    provider = ApprovalProvider()
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})
    session_id = "s4-approval"

    events = await _run(provider, session_id, user_config=user_config,
                        registry=registry)
    assert EventType.TASK_INTERRUPTED in [ev.type for ev in events]
    interrupted = next(ev for ev in events if ev.type == EventType.TASK_INTERRUPTED)
    assert interrupted.payload["state"] == TaskState.PENDING_APPROVAL.value
    assert executed == []
    values = await _checkpoint_values(graph_env / "checkpoints.db", session_id)
    assert values.get("delegation_count") == 1

    resume_events = [ev async for ev in executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry)]
    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [TOOL_ARGS], "approved tool must execute exactly once"
    values = await _checkpoint_values(graph_env / "checkpoints.db", session_id)
    assert values.get("delegation_count") == 1
