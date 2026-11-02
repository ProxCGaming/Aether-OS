"""Epic 1, E1-15: the shared agent blackboard (LangGraph state).

Pins the design the owner chose: the blackboard lives in AetherState
(not a parallel store), sections merge by rule (plan/decisions-style
overwrite vs findings/decisions append), subgraph echoes do not
duplicate entries, prompts receive a bounded briefing (never a raw
dump), the planner writes its plan to the board, and the coder's prompt
contains what the planner wrote.
"""
from types import SimpleNamespace

import pytest

from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.state import (
    BLACKBOARD_BRIEFING_MAX_CHARS,
    blackboard_briefing,
    merge_blackboard,
)

# ── Reducer rules ────────────────────────────────────────────────────

def test_plan_overwrites_and_findings_append():
    board = merge_blackboard({}, {"plan": "v1", "findings": ["a"]})
    board = merge_blackboard(board, {"plan": "v2", "findings": ["b"]})
    assert board["plan"] == "v2"
    assert board["findings"] == ["a", "b"]


def test_open_questions_overwrite_so_answered_questions_leave():
    board = merge_blackboard({}, {"open_questions": ["q1", "q2"]})
    board = merge_blackboard(board, {"open_questions": ["q2"]})
    assert board["open_questions"] == ["q2"]


def test_subgraph_echo_does_not_duplicate_appends():
    board = merge_blackboard({}, {"findings": ["a", "b"]})
    # A subgraph returns the FULL board value as its update.
    echoed = merge_blackboard(board, {"findings": ["a", "b"]})
    assert echoed["findings"] == ["a", "b"]
    # An echo plus one new entry keeps exactly the new entry appended.
    grown = merge_blackboard(board, {"findings": ["a", "b", "c"]})
    assert grown["findings"] == ["a", "b", "c"]


def test_list_cap_keeps_the_most_recent():
    board = {}
    for i in range(60):
        board = merge_blackboard(board, {"decisions": [f"d{i}"]})
    assert len(board["decisions"]) == 50
    assert board["decisions"][-1] == "d59"


# ── Bounded briefing ─────────────────────────────────────────────────

def test_briefing_empty_board_is_empty():
    assert blackboard_briefing({}) == ""
    assert blackboard_briefing({"blackboard": {}}) == ""


def test_briefing_is_bounded_no_matter_what_the_board_holds():
    board = {
        "plan": "p" * 5000,
        "findings": [f"finding-{i}-" + "x" * 200 for i in range(50)],
        "decisions": [f"decision-{i}-" + "y" * 200 for i in range(50)],
        "open_questions": ["q" * 500],
    }
    text = blackboard_briefing({"blackboard": board})
    assert len(text) <= BLACKBOARD_BRIEFING_MAX_CHARS
    assert "finding-49" in text  # the most recent entries survive


# ── Node wiring ──────────────────────────────────────────────────────

class StubProvider:
    """Records the messages it was called with; streams fixed text."""

    def __init__(self, text):
        self.text = text
        self.seen_messages = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_messages = messages
        yield SimpleNamespace(text=self.text, tool_calls=None, finish_reason="stop")


def _config(provider):
    return {"configurable": {"provider": provider}}


def _base_state(**overrides):
    state = {
        "original_prompt": "Build the thing",
        "messages": [{"role": "user", "content": "Build the thing"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "bb-test",
        "session_id": "bb-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_planner_writes_its_plan_to_the_blackboard(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "/tmp/plan.txt")
    provider = StubProvider("Step 1: scaffold. Step 2: build.")
    result = await planner_module.planner_node(_base_state(), _config(provider))
    assert result["blackboard"]["plan"] == "Step 1: scaffold. Step 2: build."
    # And through the reducer it lands on the shared board.
    board = merge_blackboard({}, result["blackboard"])
    assert board["plan"].startswith("Step 1")


@pytest.mark.asyncio
async def test_coder_prompt_contains_the_planners_blackboard_plan(monkeypatch):
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "/tmp/code.txt")
    board = merge_blackboard({}, {"plan": "Step 1: scaffold. Step 2: build."})
    board = merge_blackboard(board, {"findings": ["The API returns 404 on /old"]})
    provider = StubProvider("Done.")
    await coder_module.coder_node(_base_state(blackboard=board), _config(provider))
    system_text = provider.seen_messages[0]["content"]
    assert "Shared team blackboard" in system_text
    assert "Step 1: scaffold" in system_text
    assert "The API returns 404 on /old" in system_text
