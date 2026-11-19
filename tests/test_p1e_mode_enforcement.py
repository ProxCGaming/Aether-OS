"""P1e mode + cap enforcement at the delegation seam (K-04 hookup).

K-04 landed the delegation-settings schema and resolver; the
modes ``off`` / ``manual`` resolved but had no consumer. This
suite pins the hookup on the migrated (tools-topology)
supervisor, where the enforcement lives:

- mode ``off``: the delegation tools are not offered, a call
  that still arrives (a synthesised JSON decision, a forged
  native call) is refused inside the tool with the mode named,
  and no specialist ever runs;
- mode ``manual``: every delegation pauses for owner approval
  (the house ``approval_request`` interrupt surface, naming the
  specialist + briefing) BEFORE the specialist runs — approval
  runs it exactly once, denial never runs it;
- the per-delegation caps: a slow delegation is stopped at
  ``per_delegation_timeout_s`` and a spending one at
  ``per_delegation_token_budget`` (judged from UsageLedger rows
  recorded during the delegation window), each returning the
  honest stop text instead of a result;
- the legacy topology is deliberately untouched: it ignores the
  mode exactly as before (byte-identical behaviour), pinned by
  the last test — enforcement is a tools-topology property
  until the legacy graph is retired.

Providers are scripted per node name, on the P1d
characterisation harness's pattern.
"""

import asyncio
import json
import time

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from aether_engine import task_ledger
from aether_engine.config import UserConfig
from aether_engine.cost.ledger import UsageLedger
from aether_engine.langgraph import delegation as delegation_module
from aether_engine.langgraph import subagents as subagents_module
from aether_engine.langgraph.fanout import GROUP_TOOL_NAME
from aether_engine.langgraph.graph import compile_graph
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.state import message_dicts
from aether_engine.langgraph.supervisor_model import MEMORY_TOOL_NAMES
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

RESEARCHER_ANSWER = "Research findings: mode enforcement works."
PLANNER_ANSWER = "Plan: 1) enforce the mode, 2) join in order."
PROBE_RESULT = "mode-probe-result"
TASK_ID = "p1e2-task"

GROUP_MEMBERS = [
    {"specialist": "researcher", "task": "research the first question"},
    {"specialist": "planner", "task": "plan the second question"},
]


class ModeProvider:
    """One provider for every node, scripted per node name.

    Supervisor actions: ("json", decision) / ("native", target) /
    ("group", members) — calls the group tool, asserting it was
    offered / ("group_forged", members) — emits the group call
    without an offer (a stale or forged pending call) /
    ("text", content).
    Specialist actions: ("text", content) / ("tool", name) /
    ("sleep", seconds, content).
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
                     and n.startswith("delegate_to_")),
                    None,
                )
                assert name, f"no delegation tool offered for {target}: {offered}"
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=name, args={"briefing": "native briefing"},
                        call_id=f"native-{target}-{self.rounds('supervisor')}")],
                    finish_reason="tool_calls")
            elif kind in ("group", "group_forged"):
                if kind == "group":
                    assert GROUP_TOOL_NAME in offered, (
                        f"group tool not offered: {offered}")
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name=GROUP_TOOL_NAME,
                        args={"members": action[1]},
                        call_id=f"group-{self.rounds('supervisor')}")],
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
        elif action[0] == "sleep":
            await asyncio.sleep(action[1])
            yield StreamChunk(text=action[2], finish_reason="stop")
        else:
            yield StreamChunk(text=action[1], finish_reason="stop")


@pytest.fixture(autouse=True)
def _stub_artifacts(monkeypatch):
    for module in (researcher_module, planner_module, coder_module):
        monkeypatch.setattr(
            module, "save_artifact", lambda *a, **k: "/tmp/p1e2_artifact.txt")


@pytest.fixture
def fanout_env(tmp_path, monkeypatch):
    """Group runs execute members as S13 children: their
    checkpoints and the ledger tap need the S24 isolation."""
    monkeypatch.setattr(
        subagents_module, "SUBAGENT_CHECKPOINT_DB",
        tmp_path / "subagent_checkpoints.db")
    task_ledger.clear_tap()
    return tmp_path


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
        "task_id": TASK_ID,
        "session_id": "p1e2-session",
        "message_id": None,
        "context_briefing": None,
        "active_skills": [],
        "run_config": None,
    }


def _config(provider, *, user_config=None, registry=None):
    return {
        "recursion_limit": 25,
        "configurable": {
            "thread_id": "p1e2-thread",
            "provider": provider,
            "tool_registry": registry or ToolRegistry(),
            "user_config": user_config or UserConfig(),
            "agent_models": {},
            "agent_tools": {},
            "disabled_nodes": set(),
            "workspace_roots": [],
            "request_id": "p1e2-req",
            "session_id": "p1e2-session",
            "task_class": "standard",
            "forced_agent": None,
        },
    }


def _mode_config(mode, *, pattern="tools", **block_extra):
    block = {"mode": mode}
    block.update(block_extra)
    # supervisor_pattern mirrors production: the executor compiles
    # the run's graph from the same field the routing prompt reads.
    return UserConfig(delegation_settings=block, supervisor_pattern=pattern)


async def _run(tmp_path, provider, *, user_config=None, registry=None,
               pattern="tools"):
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern=pattern)
        config = _config(provider, user_config=user_config, registry=registry)
        return await graph.ainvoke(_initial_state(), config=config)


def _tool_contents(final):
    return [
        str(m.get("content") or "")
        for m in message_dicts(final.get("messages") or [])
        if m.get("role") == "tool"
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


# ── Mode off ──────────────────────────────────────────────────────

async def test_off_mode_offers_no_delegation_tools(tmp_path):
    provider = ModeProvider(
        supervisor_script=[
            ("json", {"next": "END", "reason": "Direct answer.",
                      "briefing": ""}),
        ])
    final = await _run(tmp_path, provider, user_config=_mode_config("off"))
    supervisor_calls = [c for c in provider.calls
                        if c["node_name"] == "supervisor"]
    assert supervisor_calls, "the supervisor was consulted"
    for call in supervisor_calls:
        assert not any(
            name and (name.startswith("delegate_to_")
                      or name == GROUP_TOOL_NAME)
            for name in call["tools"]), call["tools"]
        # The supervisor's own tools are unaffected: memory tools
        # are still offered in mode off.
        assert any(name in MEMORY_TOOL_NAMES for name in call["tools"])
    # The routing prompt says delegation is disabled, plainly.
    first_messages = supervisor_calls[0]["messages"]
    assert any("Delegation is DISABLED" in str(m.get("content") or "")
               for m in first_messages)
    assert provider.rounds("researcher") == 0
    assert _standing_answer(final) == "Direct answer."


async def test_off_mode_refuses_a_synthesised_json_delegation(tmp_path):
    """A JSON decision to delegate (the adapter synthesises the
    delegation call) meets the tool's refusal: the specialist
    never runs and the supervisor is told the mode named it."""
    provider = ModeProvider(
        supervisor_script=[
            ("json", {"next": "researcher", "reason": "gather facts",
                      "briefing": ""}),
            ("json", {"next": "END", "reason": "Direct answer.",
                      "briefing": ""}),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(tmp_path, provider, user_config=_mode_config("off"))
    assert "__interrupt__" not in final
    assert provider.rounds("researcher") == 0
    assert RESEARCHER_ANSWER not in _contents(final)
    assert any("delegation mode is 'off'" in content
               for content in _tool_contents(final))
    decisions = [
        entry["decision"]["next"]
        for entry in final.get("delegation_log") or []
        if isinstance(entry.get("decision"), dict)
    ]
    assert decisions == ["researcher", "END"]
    assert _standing_answer(final) == "Direct answer."


async def test_off_mode_group_call_is_refused(tmp_path, fanout_env):
    """A forged group call in mode off: no member runs in any
    scheduling — the degrade-to-sequential path must not become
    an off-mode back door."""
    provider = ModeProvider(
        supervisor_script=[
            ("group_forged", GROUP_MEMBERS),
            ("text", "Direct answer."),
        ],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "planner": [("text", PLANNER_ANSWER)],
        },
    )
    final = await _run(
        tmp_path, provider,
        user_config=_mode_config("off", max_parallel_delegations=2))
    assert provider.rounds("researcher") == 0
    assert provider.rounds("planner") == 0
    assert any("delegation mode is 'off'" in content
               for content in _tool_contents(final))
    assert _standing_answer(final) == "Direct answer."


# ── Mode manual: single delegations ───────────────────────────────

async def test_manual_mode_pauses_before_the_specialist_runs(tmp_path):
    provider = ModeProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("text", "Done."),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(provider, user_config=_mode_config("manual"))
        first = await graph.ainvoke(_initial_state(), config=config)
        interrupts = first.get("__interrupt__") or []
        assert interrupts, "a manual delegation must pause for approval"
        payload = interrupts[0].value
        assert payload.get("type") == "approval_request"
        assert payload.get("tool_name") == "delegate_to_researcher"
        assert payload.get("delegation", {}).get("specialist") == "researcher"
        assert payload.get("delegation", {}).get("briefing") == "native briefing"
        assert payload.get("args", {}).get("briefing") == "native briefing"
        assert provider.rounds("researcher") == 0, \
            "the pause lands BEFORE the specialist runs"

        final = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
    assert "__interrupt__" not in final
    assert provider.rounds("researcher") == 1, \
        "the approved delegation runs the specialist exactly once"
    assert RESEARCHER_ANSWER in _contents(final)
    assert final["delegation_count"] == 1


async def test_manual_mode_denial_never_runs_the_specialist(tmp_path):
    provider = ModeProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("json", {"next": "END",
                      "reason": "Understood, doing it myself.",
                      "briefing": ""}),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(provider, user_config=_mode_config("manual"))
        first = await graph.ainvoke(_initial_state(), config=config)
        assert first.get("__interrupt__"), "the delegation paused"
        final = await graph.ainvoke(
            Command(resume={"approved": False}), config=config)
    assert "__interrupt__" not in final
    assert provider.rounds("researcher") == 0
    assert RESEARCHER_ANSWER not in _contents(final)
    assert any("Delegation not approved" in content
               and "researcher" in content
               for content in _tool_contents(final))
    assert _standing_answer(final) == "Understood, doing it myself."


async def test_manual_gate_then_specialist_tool_approval(tmp_path):
    """Two pauses, one delegation: the manual gate first (the
    specialist has not started), then the specialist's own tool
    approval inside its run — the gate is not re-asked on the
    second resume, and the delegation is counted once."""
    executed = []

    def _probe(q: str = "") -> str:
        executed.append(q)
        return PROBE_RESULT

    registry = ToolRegistry()
    registry.register(Tool(
        name="char_probe",
        description="Mode-enforcement probe (approval-gated).",
        input_schema={"type": "object",
                      "properties": {"q": {"type": "string"}}},
        execute_fn=_probe,
        risk=ToolRisk.READ_ONLY,
    ))
    provider = ModeProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("text", "Done."),
        ],
        specialist_scripts={
            "researcher": [("tool", "char_probe"), ("text", RESEARCHER_ANSWER)],
        },
    )
    user_config = UserConfig(
        delegation_settings={"mode": "manual"},
        tool_policies={"char_probe": "Require Approval"})
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(provider, user_config=user_config, registry=registry)
        first = await graph.ainvoke(_initial_state(), config=config)
        payload = (first.get("__interrupt__") or [None])[0]
        assert payload is not None
        assert payload.value.get("tool_name") == "delegate_to_researcher"
        assert provider.rounds("researcher") == 0
        assert executed == []

        second = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
        payload = (second.get("__interrupt__") or [None])[0]
        assert payload is not None, "the specialist's tool approval pauses"
        assert payload.value.get("tool_name") == "char_probe"
        assert executed == [], "the gated tool has not executed yet"

        final = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
    assert "__interrupt__" not in final
    assert executed == ["x"], "the approved tool executes exactly once"
    assert provider.rounds("researcher") == 2
    assert final["delegation_count"] == 1
    assert RESEARCHER_ANSWER in _contents(final)


# ── Mode manual: parallel groups ──────────────────────────────────

async def test_manual_mode_group_pauses_for_one_approval(
        tmp_path, fanout_env):
    provider = ModeProvider(
        supervisor_script=[
            ("group", GROUP_MEMBERS),
            ("text", "Done."),
        ],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "planner": [("text", PLANNER_ANSWER)],
        },
    )
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(
            provider,
            user_config=_mode_config("manual", max_parallel_delegations=2))
        first = await graph.ainvoke(_initial_state(), config=config)
        interrupts = first.get("__interrupt__") or []
        assert interrupts, "a manual group must pause for approval"
        payload = interrupts[0].value
        assert payload.get("type") == "approval_request"
        assert payload.get("tool_name") == GROUP_TOOL_NAME
        members = payload.get("delegation", {}).get("members") or []
        assert [m["specialist"] for m in members] == ["researcher", "planner"]
        assert provider.rounds("researcher") == 0
        assert provider.rounds("planner") == 0

        # The legacy boolean resume form is accepted too.
        final = await graph.ainvoke(Command(resume=True), config=config)
    assert "__interrupt__" not in final
    contents = _contents(final)
    # Member answers ride the group's joined summary text.
    assert any(RESEARCHER_ANSWER in c for c in contents)
    assert any(PLANNER_ANSWER in c for c in contents)
    assert final["delegation_count"] == 2, "a group of 2 costs 2 delegations"


async def test_manual_mode_group_denial_runs_no_member(
        tmp_path, fanout_env):
    provider = ModeProvider(
        supervisor_script=[
            ("group", GROUP_MEMBERS),
            ("json", {"next": "END", "reason": "Understood.",
                      "briefing": ""}),
        ],
        specialist_scripts={
            "researcher": [("text", RESEARCHER_ANSWER)],
            "planner": [("text", PLANNER_ANSWER)],
        },
    )
    db_path = tmp_path / "p1e2.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver, supervisor_pattern="tools")
        config = _config(
            provider,
            user_config=_mode_config("manual", max_parallel_delegations=2))
        first = await graph.ainvoke(_initial_state(), config=config)
        assert first.get("__interrupt__"), "the group paused for approval"
        final = await graph.ainvoke(
            Command(resume={"approved": False}), config=config)
    assert provider.rounds("researcher") == 0
    assert provider.rounds("planner") == 0
    assert any("Delegation not approved" in content
               for content in _tool_contents(final))


# ── Per-delegation caps ───────────────────────────────────────────

async def test_timeout_stops_a_slow_delegation(tmp_path):
    provider = ModeProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("json", {"next": "END", "reason": "Done.", "briefing": ""}),
        ],
        specialist_scripts={
            "researcher": [("sleep", 5, RESEARCHER_ANSWER)],
        },
    )
    started = time.monotonic()
    final = await _run(
        tmp_path, provider,
        user_config=UserConfig(
            delegation_settings={"per_delegation_timeout_s": 1},
            supervisor_pattern="tools"))
    elapsed = time.monotonic() - started
    assert elapsed < 4, f"the delegation was not stopped promptly: {elapsed:.1f}s"
    assert RESEARCHER_ANSWER not in _contents(final)
    assert any("per-delegation time budget" in content
               and "stopped" in content
               for content in _tool_contents(final))
    assert final["delegation_count"] == 1
    assert _standing_answer(final) == "Done."


async def test_token_budget_stops_a_spending_delegation(
        tmp_path, monkeypatch):
    monkeypatch.setattr(delegation_module, "_CAP_POLL_S", 0.02)
    ledger = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(
        delegation_module, "get_usage_ledger", lambda: ledger)
    # Spend recorded BEFORE the delegation (the supervisor's own
    # earlier calls): the baseline snapshot must exclude it — the
    # budget judges only what the delegation window records.
    ledger.record_call(
        model_name="scripted-model", provider_name="scripted",
        input_tokens=5000, task_id=TASK_ID, task_class="standard")

    def _spend(q: str = "") -> str:
        ledger.record_call(
            model_name="scripted-model", provider_name="scripted",
            input_tokens=2000, task_id=TASK_ID, task_class="standard")
        return "spent"

    registry = ToolRegistry()
    registry.register(Tool(
        name="spend_probe",
        description="Records usage-ledger spend for the task.",
        input_schema={"type": "object",
                      "properties": {"q": {"type": "string"}}},
        execute_fn=_spend,
        risk=ToolRisk.READ_ONLY,
    ))
    provider = ModeProvider(
        supervisor_script=[
            ("native", "researcher"),
            ("json", {"next": "END", "reason": "Done.", "briefing": ""}),
        ],
        specialist_scripts={
            "researcher": [("tool", "spend_probe"),
                           ("sleep", 3, RESEARCHER_ANSWER)],
        },
    )
    started = time.monotonic()
    final = await _run(
        tmp_path, provider, registry=registry,
        user_config=UserConfig(
            delegation_settings={"per_delegation_token_budget": 1000},
            supervisor_pattern="tools"))
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, \
        f"the delegation was not stopped promptly: {elapsed:.1f}s"
    assert RESEARCHER_ANSWER not in _contents(final)
    stops = [c for c in _tool_contents(final) if "token budget" in c]
    assert stops, _tool_contents(final)
    assert "2,000 tokens were recorded" in stops[0], stops[0]
    assert final["delegation_count"] == 1


# ── Legacy topology: deliberately untouched ───────────────────────

@pytest.mark.parametrize("mode", ["off", "manual"])
async def test_legacy_topology_ignores_the_mode(tmp_path, mode):
    """Byte-identical legacy behaviour: the mode is enforced only
    at the tools seam, so on the legacy graph a delegation in
    mode off/manual runs exactly as it always has — no offer
    filtering, no approval pause. (The enforcement's scope is
    the migrated supervisor; the legacy graph is retired by the
    P1d flip, not retrofitted.)"""
    # Driven through the JSON protocol (ModeProvider's native
    # action speaks the tools topology's delegate_to_* names).
    provider = ModeProvider(
        supervisor_script=[
            ("json", {"next": "researcher", "reason": "gather facts",
                      "briefing": ""}),
            ("json", {"next": "END", "reason": "done", "briefing": ""}),
        ],
        specialist_scripts={"researcher": [("text", RESEARCHER_ANSWER)]},
    )
    final = await _run(
        tmp_path, provider, pattern="legacy",
        user_config=_mode_config(mode, pattern="legacy"))
    assert "__interrupt__" not in final
    assert provider.rounds("researcher") == 1
    assert RESEARCHER_ANSWER in _contents(final)


# ── Helper units ──────────────────────────────────────────────────

def test_approval_granted_conventions():
    from aether_engine.langgraph.delegation import approval_granted

    assert approval_granted(True) is True
    assert approval_granted(False) is False
    assert approval_granted({"approved": True}) is True
    assert approval_granted({"approved": False}) is False
    assert approval_granted(None) is False
