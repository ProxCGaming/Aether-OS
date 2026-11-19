"""Wave 5 craft: the specialist nodes share one runner (nodes/runner.py).

The Epic 1 suites pin the shared body (parity, plan-mode, advisor,
prewalk, compaction, S1-M5 queueing, S1-M6 artifacts). What they do not
pin end-to-end is the per-node tail: each wrapper's FULL result dict on
the plain-text path — above all the researcher's derived blackboard
finding (200-char, whitespace-collapsed; empty board entry when the
content is empty) — plus the spec/wrapper wiring itself (frozen spec,
per-specialist logger identity under aether_engine.langgraph.<name>).
"""
import dataclasses
import logging
from types import SimpleNamespace

import pytest

from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes.coder import CODER_SPEC
from aether_engine.langgraph.nodes.planner import PLANNER_SPEC
from aether_engine.langgraph.nodes.researcher import RESEARCHER_SPEC

SPECIALISTS = [
    pytest.param(coder_module, coder_module.coder_node, CODER_SPEC, id="coder"),
    pytest.param(planner_module, planner_module.planner_node, PLANNER_SPEC, id="planner"),
    pytest.param(researcher_module, researcher_module.researcher_node, RESEARCHER_SPEC, id="researcher"),
]


class StubProvider:
    """Streams fixed text; records the call for prompt/node assertions."""

    def __init__(self, text):
        self.text = text
        self.seen_messages = None
        self.seen_node_name = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_messages = messages
        self.seen_node_name = node_name
        yield SimpleNamespace(text=self.text, tool_calls=None, finish_reason="stop")


def _state(**overrides):
    state = {
        "original_prompt": "Do the task",
        "messages": [{"role": "user", "content": "Do the task"}],
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
        "task_id": "runner-test",
        "session_id": "runner-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }
    state.update(overrides)
    return state


def _config(provider):
    return {"configurable": {"provider": provider}}


def _stub_save_artifact(monkeypatch, module):
    calls = []

    def _save(task_id, kind, filename, content):
        calls.append((task_id, kind, filename, content))
        return f"/tmp/{filename}"

    monkeypatch.setattr(module, "save_artifact", _save)
    return calls


# ── Spec contract ────────────────────────────────────────────────────

def test_specs_are_frozen():
    for spec in (CODER_SPEC, PLANNER_SPEC, RESEARCHER_SPEC):
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "mutated"


@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,spec", SPECIALISTS)
async def test_wrapper_runs_its_spec(module, node, spec, monkeypatch):
    """Each wrapper streams under its own name and opens its system
    prompt with its own display name + role sentence."""
    _stub_save_artifact(monkeypatch, module)
    provider = StubProvider("done")
    await node(_state(), _config(provider))
    assert provider.seen_node_name == spec.name
    system = provider.seen_messages[0]
    assert system["role"] == "system"
    assert system["content"] == (
        f"You are a {spec.display_name} agent. {spec.role_prompt}\n")


# ── Full plain-text result shapes ────────────────────────────────────

@pytest.mark.asyncio
async def test_coder_result_shape(monkeypatch):
    calls = _stub_save_artifact(monkeypatch, coder_module)
    result = await coder_module.coder_node(
        _state(), _config(StubProvider("print('hi')")))
    # P2c step 2: the result also carries the round's offered-vs-
    # called entry for the delegation log (no registry in this
    # harness, so only the S13 spawn tool was offered).
    (offer_entry,) = result.pop("delegation_log")
    assert offer_entry["agent"] == "coder"
    assert offer_entry["tools_offered"] == ["spawn_subagent"]
    assert offer_entry["tools_called"] == []
    assert result == {
        "current_phase": "coding",
        "messages": [{"role": "assistant", "content": "print('hi')"}],
        "artifact_paths": {"coder_output": "/tmp/coder_output.txt"},
    }
    assert calls == [
        ("runner-test", "code", "coder_output.txt", "print('hi')")]


@pytest.mark.asyncio
async def test_planner_result_shape(monkeypatch):
    calls = _stub_save_artifact(monkeypatch, planner_module)
    result = await planner_module.planner_node(
        _state(), _config(StubProvider("Step 1: scaffold.")))
    # P2c step 2: offered-vs-called entry rides the result (see the
    # coder shape test).
    (offer_entry,) = result.pop("delegation_log")
    assert offer_entry["agent"] == "planner"
    assert offer_entry["tools_offered"] == ["spawn_subagent"]
    assert offer_entry["tools_called"] == []
    assert result == {
        "current_phase": "planning",
        "plan": "Step 1: scaffold.",
        "blackboard": {"plan": "Step 1: scaffold."},
        "messages": [{"role": "assistant", "content": "Step 1: scaffold."}],
        "artifact_paths": {"planner_output": "/tmp/planner_output.txt"},
    }
    assert calls == [
        ("runner-test", "plan", "planner_output.txt", "Step 1: scaffold.")]


@pytest.mark.asyncio
async def test_researcher_result_shape_and_finding_derivation(monkeypatch):
    calls = _stub_save_artifact(monkeypatch, researcher_module)
    raw = "x" * 150 + "   \n\t  " + "y" * 150
    result = await researcher_module.researcher_node(
        _state(), _config(StubProvider(raw)))
    expected_finding = "x" * 150 + " " + "y" * 49
    assert len(expected_finding) == 200
    # P2c step 2: offered-vs-called entry rides the result (see the
    # coder shape test).
    (offer_entry,) = result.pop("delegation_log")
    assert offer_entry["agent"] == "researcher"
    assert offer_entry["tools_offered"] == ["spawn_subagent"]
    assert offer_entry["tools_called"] == []
    assert result == {
        "current_phase": "researching",
        "blackboard": {"findings": [expected_finding]},
        "messages": [{"role": "assistant", "content": raw}],
        "artifact_paths": {"researcher_output": "/tmp/researcher_output.txt"},
    }
    assert calls == [
        ("runner-test", "research", "researcher_output.txt", raw)]


@pytest.mark.asyncio
async def test_researcher_empty_content_yields_empty_blackboard(monkeypatch):
    _stub_save_artifact(monkeypatch, researcher_module)
    result = await researcher_module.researcher_node(
        _state(), _config(StubProvider("")))
    assert result["blackboard"] == {}
    assert result["current_phase"] == "researching"


# ── Logger identity ──────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("module,node,spec", SPECIALISTS)
async def test_drain_logs_under_the_specialists_own_logger(
        module, node, spec, monkeypatch, caplog):
    """The runner logs as aether_engine.langgraph.<name> — the identity
    the per-node modules always used, NOT ...langgraph.nodes.<name>."""
    _stub_save_artifact(monkeypatch, module)
    queued = [
        {"name": "search_text", "args": {"pattern": "TODO"}, "call_id": "call-2"},
        {"name": "get_current_time", "args": {}, "call_id": "call-3"},
    ]
    with caplog.at_level(logging.INFO, logger=f"aether_engine.langgraph.{spec.name}"):
        result = await node(
            _state(queued_tool_calls=queued), _config(StubProvider("unused")))
    assert result == {
        "pending_tool_call": queued[0],
        "queued_tool_calls": queued[1:],
    }
    drain_records = [r for r in caplog.records if "draining queued" in r.getMessage()]
    assert len(drain_records) == 1
    assert drain_records[0].name == f"aether_engine.langgraph.{spec.name}"
    assert drain_records[0].getMessage() == (
        f"{spec.display_name} draining queued tool call call-2 "
        "(search_text); 1 still queued")
