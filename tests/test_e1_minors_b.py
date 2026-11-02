"""Epic 1 minors, state/structured cluster: E1-F13, F14, F15, F17.

- F13: the structured-output correction retry must disclose the
  configured schema itself, not only the validation errors — a model
  cannot match a format it has never been shown.
- F14: the blackboard's promised five sections (plan / findings /
  decisions / open_questions / artifacts) are ALL bounded, and the
  briefing renders artifacts plus what changed since the reading
  agent's last turn.
- F15: the auto titles the creation path actually writes — the short
  prompt verbatim, and the 30-char prefix + "..." for long prompts —
  qualify as default titles for model re-titling.
- F17: the scratchpad accepts kind="markdown" end to end.
"""
import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.state import (
    BLACKBOARD_ENTRY_MAX_CHARS,
    BLACKBOARD_LIST_CAP,
    BLACKBOARD_PLAN_MAX_CHARS,
    blackboard_briefing,
    merge_blackboard,
)
from aether_engine.providers.base import StreamChunk
from aether_engine.session_titles import is_default_title
from aether_engine.tools.registry import build_tool_registry
from aether_engine.tools.scratchpad import (
    SCRATCHPAD_KINDS,
    sanitise_scratchpad,
    scratchpad_event_payload,
)

# ── E1-F13: the correction retry discloses the schema ───────────────

PLAN_SCHEMA = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["low", "high"]},
    },
}


class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        text = self.replies.pop(0) if self.replies else ""
        yield StreamChunk(text=text, finish_reason="stop")


@pytest.fixture(autouse=True)
def _no_artifacts(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "structured/plan.txt")


@pytest.mark.asyncio
async def test_correction_retry_discloses_the_schema():
    provider = _ScriptedProvider(["here is your plan, trust me", '{"steps": ["fixed"]}'])
    cfg = {"configurable": {
        "provider": provider,
        "tool_registry": None,
        "agent_models": {},
        "agent_tools": {},
        "user_config": UserConfig(agent_output_schemas={"planner": PLAN_SCHEMA}),
    }}
    state = {"messages": [{"role": "user", "content": "plan the thing"}],
             "original_prompt": "plan the thing", "task_id": "f13-test"}
    result = await planner_node(state, cfg)
    assert result["structured_outputs"] == {"planner": {"steps": ["fixed"]}}
    assert len(provider.calls) == 2
    retry_text = " ".join(m.get("content", "") for m in provider.calls[1])
    # The enum sentinel and the required field names must be visible in
    # the retry — pre-fix neither appeared in ANY recorded call.
    assert '"risk"' in retry_text and '"low"' in retry_text and '"high"' in retry_text
    assert '"steps"' in retry_text


# ── E1-F14: five bounded sections + artifacts + per-agent deltas ────

def test_plan_and_open_questions_are_capped():
    board = merge_blackboard({}, {"plan": "p" * 100_000})
    assert len(board["plan"]) == BLACKBOARD_PLAN_MAX_CHARS
    board = merge_blackboard({}, {"open_questions": [f"q{i}" for i in range(60)]})
    assert len(board["open_questions"]) == BLACKBOARD_LIST_CAP
    assert board["open_questions"][-1] == "q59"


def test_list_entries_are_size_capped():
    board = merge_blackboard({}, {"findings": ["x" * 5000]})
    assert len(board["findings"][0]) == BLACKBOARD_ENTRY_MAX_CHARS


def test_artifacts_section_appends_caps_and_survives_echo():
    board = merge_blackboard({}, {"artifacts": ["a.txt"]})
    board = merge_blackboard(board, {"artifacts": ["b.txt"]})
    assert board["artifacts"] == ["a.txt", "b.txt"]
    echoed = merge_blackboard(board, {"artifacts": ["a.txt", "b.txt"]})
    assert echoed["artifacts"] == ["a.txt", "b.txt"]
    for i in range(60):
        board = merge_blackboard(board, {"artifacts": [f"f{i}.txt"]})
    assert len(board["artifacts"]) == BLACKBOARD_LIST_CAP


def test_briefing_includes_artifacts():
    board = merge_blackboard({}, {"artifacts": ["ARTIFACT-SENTINEL.txt"]})
    text = blackboard_briefing({"blackboard": board})
    assert "ARTIFACT-SENTINEL.txt" in text
    assert "Artifacts" in text


def test_briefing_delta_since_the_reading_agents_last_turn():
    state = {"task_id": "f14-delta-task", "active_specialist": "coder", "blackboard": {}}
    state["blackboard"] = merge_blackboard({}, {"findings": ["first finding"]})
    blackboard_briefing(state)  # coder's first turn: baseline, no delta
    state["blackboard"] = merge_blackboard(
        state["blackboard"], {"decisions": ["DELTA-DECISION"]})
    text = blackboard_briefing(state)
    assert "since your last turn" in text
    assert "DELTA-DECISION" in text
    # No changes since: the delta segment is gone again.
    assert "since your last turn" not in blackboard_briefing(state)
    # Another agent's first briefing in this task has no delta either.
    state["active_specialist"] = "planner"
    assert "since your last turn" not in blackboard_briefing(state)


def test_briefing_stays_bounded_with_artifacts_and_delta():
    board = {}
    for i in range(30):
        board = merge_blackboard(board, {"artifacts": [f"artifact-{i}-" + "z" * 300]})
    state = {"task_id": "f14-bounded-task", "active_specialist": "coder",
             "blackboard": board}
    blackboard_briefing(state)
    state["blackboard"] = merge_blackboard(board, {"findings": ["late finding"]})
    text = blackboard_briefing(state)
    assert len(text) <= 1200


# ── E1-F15: the real auto-title shapes qualify ──────────────────────

def test_short_prompt_verbatim_title_is_a_default():
    # START_TASK stores the prompt itself when it is short (<= 30 chars).
    assert is_default_title("Hello", "Hello") is True
    assert is_default_title("help me debug this", "help me debug this") is True


def test_long_prompt_ellipsis_title_is_a_default():
    prompt = "help me debug the flaky parser test that only fails on Tuesdays"
    auto = prompt[:30] + "..."  # the exact shape routes/websocket.py writes
    assert is_default_title(auto, prompt) is True


def test_real_titles_still_do_not_qualify():
    assert is_default_title("Parser debugging session",
                            "help me debug the flaky parser test") is False
    assert is_default_title("My renamed chat", "help me debug the flaky parser test") is False


# ── E1-F17: scratchpad markdown kind ────────────────────────────────

def test_markdown_is_an_accepted_scratchpad_kind():
    assert "markdown" in SCRATCHPAD_KINDS
    payload = scratchpad_event_payload({"kind": "markdown", "content": "**bold**"}, "coder")
    assert payload == {"kind": "markdown", "content": "**bold**", "agent": "coder"}
    assert sanitise_scratchpad("markdown", "# Title\n\ntext") == "# Title\n\ntext"


def test_markdown_kind_reaches_the_tool_schema_and_executes():
    registry = build_tool_registry(include_file_tools=False, include_web_tools=False)
    tool = registry.get_tool("scratchpad_render")
    kind_enum = tool.input_schema["properties"]["kind"]["enum"]
    assert "markdown" in kind_enum
    result = tool.execute_fn(kind="markdown", content="**bold**")
    assert "Scratchpad updated with a markdown rendering" in result
