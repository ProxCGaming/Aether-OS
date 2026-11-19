"""P1d characterisation tests: supervisor behaviour, both topologies.

Plan P1d step 1/3: golden tests over the supervisor's delegation
behaviour — written against the CURRENT ``create_supervisor`` graph
("legacy") and run unchanged against the subagents-as-tools graph
("tools") as the differential pass. Every assertion here is about
behaviour (decisions logged, the authoritative delegation counter,
what the specialist produced, pause/resume), never about which
library built the graph, so the same file must pass identically
under both patterns.

The one deliberate exception is
``test_delegation_message_shape_is_the_known_delta``: the message
shape of a delegation in the final state IS the migration's known
difference (legacy strips the transfer message; the tool pattern
keeps the delegate call plus its tool result), and the test pins
each pattern's shape so the difference stays explicit.

Providers are scripted by node name. A "native" supervisor action
calls whichever delegation tool the run actually offers
(``transfer_to_*`` on legacy, ``delegate_to_*`` on tools) — the way
a real model behaves; a "json" action answers with the supervisor's
JSON decision text, exercising the no-native-tool-calling protocol.
"""

import json
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.graph import compile_graph
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.state import message_dicts
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

RESEARCHER_ANSWER = "Research findings: the spike works."
PLANNER_ANSWER = "Plan: 1) write the code, 2) ship it."
CODER_ANSWER = "Implementation complete: the code is written."
PROBE_RESULT = "char-probe-result"


class ScriptedProvider:
    """One provider for every node, scripted per node name.

    Supervisor actions (consumed in order, one per supervisor call):
      ("json", decision)  — answer with the JSON decision text
      ("native", target)  — natively call the offered delegation tool
      ("text", content)   — answer with plain text
    Specialist actions are per-agent queues of:
      ("text", content)   — answer with text
      ("tool", name)      — call a registry tool, then continue
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

    def rounds(self, node_name):
        return sum(1 for c in self.calls if c["node_name"] == node_name)

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
                yield StreamChunk(text=json.dumps(action[1]),
                                  finish_reason="stop")
            elif kind == "text":
                yield StreamChunk(text=action[1], finish_reason="stop")
            elif kind == "native":
                target = action[1]
                name = next(
                    (n for n in offered
                     if n and n.endswith(target)
                     and n.startswith(("transfer_to_", "delegate_to_"))),
                    None,
                )
                assert name, f"no delegation tool offered for {target}: {offered}"
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
        if action[0] == "tool":
            yield StreamChunk(
                tool_calls=[ToolCall(name=action[1], args={"q": "x"},
                                     call_id=f"{node_name}-tool-1")],
                finish_reason="tool_calls")
        else:
            yield StreamChunk(text=action[1], finish_reason="stop")


@pytest.fixture(params=["legacy", "tools"])
def pattern(request):
    return request.param


@pytest.fixture(autouse=True)
def _stub_artifacts(monkeypatch):
    for module in (researcher_module, planner_module, coder_module):
        monkeypatch.setattr(
            module, "save_artifact", lambda *a, **k: "/tmp/p1d_artifact.txt")


def _initial_state(prompt="do the work"):
    return {
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
        "task_id": "p1d-task",
        "session_id": "p1d-session",
        "message_id": None,
        "context_briefing": None,
        "active_skills": [],
        "run_config": None,
    }


def _config(provider, *, user_config=None, registry=None, forced_agent=None):
    return {
        "recursion_limit": 25,
        "configurable": {
            "thread_id": "p1d-thread",
            "provider": provider,
            "tool_registry": registry or ToolRegistry(),
            "user_config": user_config or UserConfig(),
            "agent_models": {},
            "agent_tools": {},
            "disabled_nodes": set(),
            "workspace_roots": [],
            "request_id": "p1d-req",
            "session_id": "p1d-session",
            "task_class": "standard",
            "forced_agent": forced_agent,
        },
    }


async def _run(pattern, tmp_path, provider, **config_kwargs):
    db_path = tmp_path / "p1d.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern=pattern)
        config = _config(provider, **config_kwargs)
        return await graph.ainvoke(_initial_state(), config=config)


def _decisions(final):
    return [
        entry["decision"]["next"]
        for entry in final.get("delegation_log") or []
        if isinstance(entry, dict) and isinstance(entry.get("decision"), dict)
    ]


def _contents(final):
    return [
        str(m.get("content") or "")
        for m in message_dicts(final.get("messages") or [])
    ]


def _standing_answer(final):
    for msg in reversed(message_dicts(final.get("messages") or [])):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return ""


# ── Single delegation, both decision protocols ──────────────────────

async def test_single_delegation_json_protocol(pattern, tmp_path):
    """A model without native tool-calling routes via JSON text."""
    provider = ScriptedProvider(
        supervisor_script=[
            ("json", {"next": "researcher", "reason": "gather facts",
                      "briefing": ""}),
            ("json", {"next": "END", "reason": "done", "briefing": ""}),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(pattern, tmp_path, provider)
    assert provider.rounds("researcher") == 1
    assert _decisions(final) == ["researcher", "END"]
    assert final["delegation_count"] == 1
    assert RESEARCHER_ANSWER in _contents(final)


async def test_single_delegation_native_protocol(pattern, tmp_path):
    """A tool-calling model delegates via the delegation tool call."""
    provider = ScriptedProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("text", "Done."),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(pattern, tmp_path, provider)
    assert provider.rounds("researcher") == 1
    assert _decisions(final) == ["researcher", "END"]
    assert final["delegation_count"] == 1
    assert RESEARCHER_ANSWER in _contents(final)


# ── Multi-specialist chain ──────────────────────────────────────────

async def test_planner_then_coder_chain(pattern, tmp_path):
    provider = ScriptedProvider(
        supervisor_script=[
            ("native", "planner"),
            ("native", "coder"),
            ("text", "All done."),
        ],
        specialist_scripts={
            "planner": [("text", PLANNER_ANSWER)],
            "coder": [("text", CODER_ANSWER)],
        },
    )
    final = await _run(pattern, tmp_path, provider)
    assert _decisions(final) == ["planner", "coder", "END"]
    assert final["delegation_count"] == 2
    contents = _contents(final)
    assert PLANNER_ANSWER in contents
    assert CODER_ANSWER in contents


# ── Delegation cap ──────────────────────────────────────────────────

async def test_delegation_cap_stops_the_chain(pattern, tmp_path):
    """Cap 2 against an always-delegating supervisor: two delegations
    execute, the third is refused at the hook and the run ends with
    the latest specialist answer standing (the S4 cap surface)."""
    provider = ScriptedProvider(
        supervisor_script=[("native", "researcher")] * 6,
        specialist_scripts={
            "researcher": [("text", f"Research answer {i}") for i in range(1, 7)],
        },
    )
    final = await _run(
        pattern, tmp_path, provider,
        user_config=UserConfig(supervisor_delegation_cap=2))
    assert final["delegation_count"] == 2
    assert _decisions(final) == ["researcher", "researcher", "END"]
    assert _standing_answer(final) == "Research answer 2"


# ── Pause inside a specialist + resume ──────────────────────────────

async def test_pause_inside_specialist_and_resume(pattern, tmp_path):
    executed = []

    def _probe(q: str = "") -> str:
        executed.append(q)
        return PROBE_RESULT

    registry = ToolRegistry()
    registry.register(Tool(
        name="char_probe",
        description="Characterisation probe (approval-gated).",
        input_schema={"type": "object",
                      "properties": {"q": {"type": "string"}}},
        execute_fn=_probe,
        risk=ToolRisk.READ_ONLY,
    ))
    provider = ScriptedProvider(
        supervisor_script=[("native", "researcher")],
        specialist_scripts={
            "researcher": [("tool", "char_probe"), ("text", RESEARCHER_ANSWER)],
        },
    )
    db_path = tmp_path / "p1d.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern=pattern)
        config = _config(
            provider, registry=registry,
            user_config=UserConfig(
                tool_policies={"char_probe": "Require Approval"}))
        first = await graph.ainvoke(_initial_state(), config=config)
        interrupts = first.get("__interrupt__") or []
        assert interrupts, "the approval pause must surface on the outer graph"
        payload = interrupts[0].value
        assert payload.get("type") == "approval_request"
        assert payload.get("tool_name") == "char_probe"

        final = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
    assert "__interrupt__" not in final
    assert executed == ["x"], "the approved tool executes exactly once"
    assert provider.rounds("researcher") == 2, \
        "the resume continues the same specialist's next round"
    assert final["delegation_count"] == 1, \
        "a delegation paused for approval is counted exactly once"
    assert RESEARCHER_ANSWER in _contents(final)


# ── Forced run: the end-task guard ──────────────────────────────────

async def test_forced_run_ends_on_the_specialists_answer(pattern, tmp_path):
    """A forced run delegates once, locally, and the specialist's
    answer stands — the supervisor provider is never consulted."""
    provider = ScriptedProvider(
        supervisor_script=[("native", "coder")],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(pattern, tmp_path, provider, forced_agent="researcher")
    assert provider.rounds("supervisor") == 0
    assert provider.rounds("researcher") == 1
    assert final["delegation_count"] == 1
    assert _standing_answer(final) == RESEARCHER_ANSWER


# ── Through the executor: events + completion (item 6) ─────────────

async def test_executor_journey_events_and_completion(pattern, tmp_path,
                                                      monkeypatch):
    """The full executor path on either topology: the chain completes,
    the response is the coder's work, and the delegation transitions
    surface as the same NODE_ACTIVITY events the UI consumes."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    provider = ScriptedProvider(
        supervisor_script=[
            ("json", {"next": "planner", "reason": "plan it first",
                      "briefing": ""}),
            ("json", {"next": "coder", "reason": "implement the plan",
                      "briefing": ""}),
            ("json", {"next": "END", "reason": "chain finished",
                      "briefing": ""}),
        ],
        specialist_scripts={
            "planner": [("text", PLANNER_ANSWER)],
            "coder": [("text", CODER_ANSWER)],
        },
    )
    events = [event async for event in executor_module.run_langgraph_task(
        "do the work",
        provider,
        tools=ToolRegistry(),
        task_id="task-p1d-executor",
        request_id="req-p1d-executor",
        session_id="p1d-executor",
        user_config=UserConfig(supervisor_pattern=pattern),
    )]
    types = [ev.type for ev in events]
    assert EventType.TASK_FAILED not in types
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert CODER_ANSWER in completed.payload["response"]
    delegations = [
        ev.payload for ev in events
        if ev.type == EventType.NODE_ACTIVITY
        and (ev.payload or {}).get("action") == "delegation"
    ]
    assert [d["to_node"] for d in delegations] == ["planner", "coder"]
    assert [d["delegation_count"] for d in delegations] == [1, 2]


# ── The known shape delta, pinned deliberately ──────────────────────

async def test_delegation_message_shape_is_the_known_delta(pattern, tmp_path):
    provider = ScriptedProvider(
        supervisor_script=[("native", "researcher"), ("text", "Done.")],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(pattern, tmp_path, provider)
    dicts = message_dicts(final.get("messages") or [])
    delegation_calls = [
        tc for m in dicts for tc in (m.get("tool_calls") or [])
        if (tc.get("function", {}).get("name") or "").startswith(
            ("transfer_to_", "delegate_to_"))
    ]
    if pattern == "legacy":
        # create_supervisor consumes the transfer message: no
        # delegation call remains in the final history.
        assert delegation_calls == []
    else:
        # The tool pattern keeps the delegate call, answered by the
        # delegation tool's result message.
        assert len(delegation_calls) == 1
        call_id = delegation_calls[0].get("id")
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == call_id
            for m in dicts)
