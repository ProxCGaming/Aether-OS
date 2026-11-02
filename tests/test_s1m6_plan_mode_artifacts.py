"""S1-M6: plan mode must not write artifacts to disk.

Plan mode is a read-only, propose-only mode (E1-09), yet every
specialist's text-completion path unconditionally called
``save_artifact(...)`` — writing coder_output.txt / planner_output.txt
/ researcher_output.txt to disk even when the user only asked for a
plan. The fix skips the save in plan mode; the content is already
returned inline (messages, plus the planner's plan/blackboard), so
nothing is lost. In plan mode the result also OMITS ``artifact_paths``
entirely: no consumer reads a specialist's artifact_paths (the
executor only seeds the channel with {}), and leaving the key out
keeps the channel's dict[str, str] typing intact. Normal mode is
byte-identical to before.
"""
import pytest

from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.providers.base import StreamChunk

SPECIALISTS = [
    pytest.param(coder_module, coder_module.coder_node, "coder_output", id="coder"),
    pytest.param(planner_module, planner_module.planner_node, "planner_output", id="planner"),
    pytest.param(researcher_module, researcher_module.researcher_node, "researcher_output", id="researcher"),
]

TEXT = "finished work product"


class TextProvider:
    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        yield StreamChunk(text=TEXT, finish_reason="stop")


def _state(**overrides):
    state = {
        "original_prompt": "Do the thing",
        "messages": [{"role": "user", "content": "Do the thing"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "s1m6-test",
        "session_id": "s1m6-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }
    state.update(overrides)
    return state


def _config():
    return {"configurable": {"provider": TextProvider()}}


@pytest.fixture
def artifact_recorder(monkeypatch):
    """Install a save_artifact recorder on ONE specialist module."""
    calls = []

    def install(module):
        def _record(*args, **kwargs):
            calls.append(args)
            return "/tmp/recorded-artifact.txt"

        monkeypatch.setattr(module, "save_artifact", _record)

    return calls, install


@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,artifact_key", SPECIALISTS)
async def test_plan_mode_never_saves_an_artifact(module, node, artifact_key, artifact_recorder):
    calls, install = artifact_recorder
    install(module)
    result = await node(_state(plan_mode=True), _config())
    assert calls == []  # nothing written to disk
    assert "artifact_paths" not in result  # and no path reported
    # The content still comes back inline.
    assert result["messages"][0]["content"] == TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,artifact_key", SPECIALISTS)
async def test_normal_mode_saves_artifact_as_before(module, node, artifact_key, artifact_recorder):
    calls, install = artifact_recorder
    install(module)
    result = await node(_state(plan_mode=False), _config())
    assert len(calls) == 1
    assert result["artifact_paths"] == {artifact_key: "/tmp/recorded-artifact.txt"}


@pytest.mark.asyncio
async def test_planner_plan_mode_still_returns_plan_inline(artifact_recorder):
    calls, install = artifact_recorder
    install(planner_module)
    result = await planner_module.planner_node(_state(plan_mode=True), _config())
    assert calls == []
    assert result["plan"] == TEXT
    assert result["blackboard"]["plan"] == TEXT
