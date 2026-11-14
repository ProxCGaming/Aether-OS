"""Master build S12: silent delegation — delegation is a state, not
chat text (audit G3, plan R6).

Before S12 the executor translated every delegation transition into
a "[Supervisor] -> Delegating to X" text line: on the run path it was
written into the episode's thinking log (next to a proper structured
NODE_ACTIVITY), and on the resume path it was emitted as a
TASK_PROGRESS text delta — with literal "\\n" escape characters, so
the UI's prefix filter never matched it and the chatter rendered
live in resumed tasks. The finish decision also produced a spurious
"Delegating to END" activity card.

S12 makes the transition purely structural on BOTH paths:

- the only signal is the NODE_ACTIVITY event (existing type, no new
  contract), carrying the specialist transition and the S4
  ``delegation_count`` read from graph state;
- END / memory-step bookkeeping entries emit no activity at all
  (the same exclusion the S4 counter applies);
- a supervisor message carrying a handoff tool call never enters
  the answer stream (its content is the delegation reason and
  already rides the activity payload);
- the S3 ledger tap, which consumes NODE_ACTIVITY only and never
  parsed the text line, now also sees resume-path delegations —
  the tap's record shape and the summary contents are unchanged
  (the summary's checkpoint leg is still preferred and holds the
  same delegations);
- the E1-11 denial explanation remains genuine user-facing text:
  when no specialist runs, the explanation IS the response.
"""
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine import task_ledger
from aether_engine.config import UserConfig
from aether_engine.cost.ledger import UsageLedger
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.executor import (
    _delegation_activity_event,
    _is_handoff_message,
)
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

PLAN_TEXT = "Plan: 1) write the code, 2) ship it."
CODE_TEXT = "Implementation complete: the code is written."
FINAL_TEXT = "Specialist final answer."
TOOL_NAME = "s12_probe"
TOOL_ARGS = {"q": "x"}

CHATTER_MARKERS = ("[Supervisor]", "Delegating to", "BLOCKED by spawn policy")


def _progress_texts(events):
    return [
        (ev.payload or {}).get("text_delta") or ""
        for ev in events if ev.type == EventType.TASK_PROGRESS
    ]


def _assert_no_chatter(events):
    for text in _progress_texts(events):
        for marker in CHATTER_MARKERS:
            assert marker not in text, f"chatter leaked: {text!r}"


def _delegation_activities(events):
    return [
        ev.payload for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and (ev.payload or {}).get("action") in ("delegation", "delegation_denied")
    ]


class ChainProvider:
    """Supervisor script: planner -> coder -> END (JSON decisions)."""

    def __init__(self):
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
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


PROSE = "Let me hand this to the planner who will break it down."


class NativeTransferProvider:
    """Native routing: prose + a transfer_to_planner call, then END."""

    def __init__(self):
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            yield StreamChunk(text=PLAN_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            yield StreamChunk(
                text=PROSE,
                tool_calls=[ToolCall(name="transfer_to_planner", args={},
                                     call_id="call-s12-1")],
                finish_reason="tool_calls")
        else:
            yield StreamChunk(
                text='{"next": "END", "reason": "done", "briefing": ""}',
                finish_reason="stop")


class ResumeChainProvider:
    """Run: supervisor -> planner, planner's tool call pauses for
    approval. Resume: planner finishes, supervisor -> coder, coder
    finishes, supervisor -> END (a genuine resume-time delegation)."""

    def __init__(self):
        self._supervisor_calls = 0
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-s12-tool")],
                    finish_reason="tool_calls")
            else:
                yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        if node_name == "coder":
            yield StreamChunk(text=CODE_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "probe it", "briefing": ""}'
        elif self._supervisor_calls == 2:
            text = '{"next": "coder", "reason": "now build it", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "done", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "s12/plan.txt")
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "s12/code.txt")
    registry = ToolRegistry()
    executed = []

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-s12"

    registry.register(Tool(
        name=TOOL_NAME, description="S12 probe tool",
        execute_fn=_fn, risk=ToolRisk.READ_ONLY))
    return tmp_path, registry, executed


async def _run(provider, session_id, user_config=None, registry=None):
    return [ev async for ev in executor_module.run_langgraph_task(
        "do the work",
        provider,
        tools=registry or ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config or UserConfig(),
    )]


# ── The helper: what counts as a delegation ──────────────────────────

def test_activity_event_excludes_finish_and_memory_steps():
    end_entry = {"decision": {"next": "END", "reason": "done"}}
    assert _delegation_activity_event(
        "t", "r", end_entry, delegation_count=2,
        user_prompt="p", updated_plan="") is None
    memory_entry = {"decision": {"next": "supervisor", "reason": "memory step"}}
    assert _delegation_activity_event(
        "t", "r", memory_entry, delegation_count=0,
        user_prompt="p", updated_plan="") is None
    assert _delegation_activity_event(
        "t", "r", {}, delegation_count=0,
        user_prompt="p", updated_plan="") is None
    assert _delegation_activity_event(
        "t", "r", None, delegation_count=0,
        user_prompt="p", updated_plan="") is None


def test_activity_event_carries_transition_and_counter():
    entry = {"decision": {"next": "Planner", "reason": "plan first",
                          "briefing": "b"}}
    ev = _delegation_activity_event(
        "task-1", "req-1", entry, delegation_count=1,
        user_prompt="do it", updated_plan="the plan")
    assert ev.type == EventType.NODE_ACTIVITY
    assert ev.payload["from_node"] == "supervisor"
    assert ev.payload["to_node"] == "planner"
    assert ev.payload["action"] == "delegation"
    assert ev.payload["delegation_count"] == 1
    assert ev.payload["output_payload"]["assigned_specialist"] == "planner"


def test_activity_event_omits_count_when_unknown():
    entry = {"decision": {"next": "coder", "reason": "build"}}
    ev = _delegation_activity_event(
        "t", "r", entry, delegation_count=None,
        user_prompt="p", updated_plan="")
    assert "delegation_count" not in ev.payload


def test_is_handoff_message_shapes():
    # OpenAI-ish nested function shape (run-path translation reads it).
    assert _is_handoff_message({"role": "assistant", "tool_calls": [
        {"function": {"name": "transfer_to_planner"}}]})
    # LangChain flat shape.
    assert _is_handoff_message({"role": "assistant", "tool_calls": [
        {"name": "transfer_to_coder", "args": {}, "id": "c1"}]})
    # A memory tool call is not a handoff — its prose is user-facing.
    assert not _is_handoff_message({"role": "assistant", "tool_calls": [
        {"name": "search_memory", "args": {}, "id": "c2"}]})
    assert not _is_handoff_message({"role": "assistant", "content": "hi"})
    assert not _is_handoff_message({"role": "assistant", "tool_calls": []})
    assert not _is_handoff_message("not a dict")


# ── Run path: no chatter anywhere user-facing ────────────────────────

@pytest.mark.asyncio
async def test_run_chain_has_no_chatter_and_ordered_activities(graph_env):
    events = await _run(ChainProvider(), "s12-chain")
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    _assert_no_chatter(events)

    activities = _delegation_activities(events)
    assert [(a["to_node"], a["delegation_count"]) for a in activities] == [
        ("planner", 1), ("coder", 2)]
    # The finish decision produces NO activity card (pre-S12 it
    # emitted a spurious delegation to "end").
    assert not any(
        (ev.payload or {}).get("to_node") == "end"
        for ev in events if ev.type == EventType.NODE_ACTIVITY)

    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    response = completed.payload["response"]
    for marker in CHATTER_MARKERS:
        assert marker not in response
    assert PLAN_TEXT in response and CODE_TEXT in response
    # The recorded thoughts (what the UI accordion replays) carry the
    # structured activities and no delegation text thought.
    thoughts = completed.payload.get("thoughts") or []
    assert any(t.get("type") == "NODE_ACTIVITY" for t in thoughts)
    for t in thoughts:
        assert "[Supervisor]" not in str(t.get("text") or "")


@pytest.mark.asyncio
async def test_episode_thinking_log_has_no_chatter(graph_env):
    await _run(ChainProvider(), "s12-episode")
    call = episodic_module.store_episode.call_args
    assert call is not None
    # The whole recorded call is the thinking log's destination —
    # no delegation narration may be persisted into the episode.
    assert "[Supervisor]" not in str(call)
    assert "Delegating to" not in str(call)


@pytest.mark.asyncio
async def test_native_transfer_prose_never_enters_the_answer_stream(graph_env):
    events = await _run(NativeTransferProvider(), "s12-native")
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]
    _assert_no_chatter(events)
    for text in _progress_texts(events):
        assert PROSE not in text
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert PROSE not in completed.payload["response"]
    # The prose is not lost: it is the delegation's reason, carried
    # on the structured activity where routing rationale belongs.
    activities = _delegation_activities(events)
    assert len(activities) == 1
    assert activities[0]["to_node"] == "planner"
    assert activities[0]["reason"] == PROSE
    assert activities[0]["delegation_count"] == 1
    # And the transfer call never faked a tool activity card.
    assert not any(
        (ev.payload or {}).get("tool_name", "").startswith("transfer_to_")
        for ev in events if ev.type == EventType.TOOL_ACTIVITY)


@pytest.mark.asyncio
async def test_denied_delegation_keeps_its_explanation_as_the_response(graph_env):
    _tmp, registry, _executed = graph_env
    events = await _run(
        ChainProvider(), "s12-denied",
        user_config=UserConfig(supervisor_delegation_cap=0),
        registry=registry)
    activities = _delegation_activities(events)
    assert len(activities) == 1
    assert activities[0]["action"] == "delegation_denied"
    assert activities[0]["to_node"] == "planner"
    assert activities[0]["delegation_count"] == 0
    # E1-11 unchanged: the explanation is genuine user-facing text —
    # it is in the progress stream and in the final response.
    assert any("blocked by the spawn policy" in t for t in _progress_texts(events))
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert "blocked by the spawn policy" in completed.payload["response"]
    # …but the "[Supervisor] ->" narration form is gone everywhere.
    for text in _progress_texts(events):
        assert "[Supervisor]" not in text


# ── Resume path: structured, and the ledger sees it ──────────────────

class _EmptyBackend:
    def list_episodes(self, limit=50):
        return []


@pytest.mark.asyncio
async def test_pause_resume_delegation_is_structural_and_tapped(graph_env):
    tmp_path, registry, executed = graph_env
    provider = ResumeChainProvider()
    session_id = "s12-pause-resume"
    task_id = f"task-{session_id}"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    # RUN: delegate to planner, planner's tool call pauses (S2
    # semantics: TOOL_APPROVAL_REQUEST + TASK_INTERRUPTED).
    events = await _run(provider, session_id, user_config=user_config,
                        registry=registry)
    assert EventType.TOOL_APPROVAL_REQUEST in [ev.type for ev in events]
    assert EventType.TASK_INTERRUPTED in [ev.type for ev in events]
    assert executed == []
    _assert_no_chatter(events)

    tap = task_ledger.tap_snapshot(task_id)
    assert tap is not None and tap["state"] == "PENDING_APPROVAL"
    assert [d["agent"] for d in tap["delegations"]] == ["planner"]

    # RESUME: the approved tool runs, planner finishes, and the
    # supervisor delegates AGAIN (to coder) after the resume — the
    # transition this slice exists for: pre-S12 it arrived as a
    # TASK_PROGRESS text delta with literal "\n" escapes.
    resume_events = [ev async for ev in executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry)]
    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [TOOL_ARGS]
    _assert_no_chatter(resume_events)

    activities = _delegation_activities(resume_events)
    assert [(a["to_node"], a["delegation_count"]) for a in activities] == [
        ("coder", 2)]
    # No resume activity for the finish decision either.
    assert not any(
        (ev.payload or {}).get("to_node") == "end"
        for ev in resume_events if ev.type == EventType.NODE_ACTIVITY)

    # The ledger tap now records the resume delegation directly
    # (source executor_event, same record shape as the run path's)…
    tap = task_ledger.tap_snapshot(task_id)
    assert tap["state"] == "SUCCEEDED"
    resume_records = [
        d for d in tap["delegations"] if d["agent"] == "coder"]
    assert resume_records and resume_records[0]["source"] == "executor_event"

    # …and the summary is unchanged in content: the checkpoint leg
    # is still preferred and reports the same two delegations.
    summary = await task_ledger.task_summary(
        task_id, session_id=session_id, backend=_EmptyBackend(),
        ledger=UsageLedger(tmp_path / "usage.db"),
        audit_path=tmp_path / "no-audit.log",
        checkpoint_db=tmp_path / "checkpoints.db")
    assert summary["found"] is True
    assert [d["agent"] for d in summary["delegations"]] == ["planner", "coder"]
    assert summary["delegation_count"] == 2
    assert summary["state"] == "SUCCEEDED"
