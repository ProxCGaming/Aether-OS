"""P2c steps 1-2 (orchestration programme, slice K-02): per-agent
tool ceilings (Filter 1) and offered-vs-called logging.

Golden safety property: a researcher delegation's offered tool set
provably excludes every write / destructive / execute tool in the
real registry — including MCP-sourced tools, which default to
EXECUTE. Filter 3 (the policy/approval execution gate) is untouched
by this slice and is not re-tested here.
"""
from types import SimpleNamespace

import pytest

from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.tools.ceilings import (
    DISABLED_DESCRIPTION,
    apply_tool_ceiling,
    ceiling_for,
)
from aether_engine.tools.registry import ToolRisk, build_tool_registry


def _defs(*names):
    return [
        {"type": "function",
         "function": {"name": n, "description": f"{n} desc", "parameters": {}}}
        for n in names
    ]


def _names(definitions):
    return [d["function"]["name"] for d in definitions]


_RISKS = {
    "read_file": ToolRisk.READ_ONLY,
    "search_web": ToolRisk.READ_ONLY,
    "write_file": ToolRisk.WRITE,
    "edit_file": ToolRisk.WRITE,
    "delete_file": ToolRisk.DESTRUCTIVE,
    "execute_shell": ToolRisk.EXECUTE,
    "mcp_unknown_tool": ToolRisk.EXECUTE,  # an MCP tool that labelled nothing
}


class TestCeilings:
    def test_researcher_offered_only_read_only(self):
        out = apply_tool_ceiling("researcher", _defs(*_RISKS), _RISKS)
        assert _names(out) == ["read_file", "search_web"]

    def test_researcher_real_registry_golden(self):
        """The headline guarantee, against the shipped registry."""
        registry = build_tool_registry()
        risk_by_name = {t.name: t.risk for t in registry.get_tools()}
        out = apply_tool_ceiling(
            "researcher", registry.get_definitions(), risk_by_name)
        offered = _names(out)
        assert offered  # the researcher still gets its read/search set
        for name in offered:
            assert risk_by_name[name] == ToolRisk.READ_ONLY, name
        # Spot-check the dangerous names explicitly.
        for dangerous in ("write_file", "edit_file", "delete_file",
                          "execute_shell"):
            assert dangerous not in offered

    def test_coder_offer_unchanged(self):
        defs = _defs(*_RISKS)
        out = apply_tool_ceiling("coder", defs, _RISKS)
        assert _names(out) == _names(defs)

    def test_planner_scratchpad_by_prefix_not_general_write(self):
        risks = dict(_RISKS, scratchpad_write=ToolRisk.WRITE)
        out = apply_tool_ceiling(
            "planner",
            _defs("read_file", "scratchpad_write", "write_file"),
            risks)
        assert _names(out) == ["read_file", "scratchpad_write"]

    def test_unknown_risk_fails_closed(self):
        out = apply_tool_ceiling(
            "researcher", _defs("read_file", "mystery_tool"),
            {"read_file": ToolRisk.READ_ONLY})
        assert _names(out) == ["read_file"]

    def test_agent_without_ceiling_unfiltered(self):
        assert ceiling_for("no_such_agent") is None
        defs = _defs(*_RISKS)
        out = apply_tool_ceiling("no_such_agent", defs, _RISKS)
        assert _names(out) == _names(defs)


class TestLegacyIntersection:
    def test_legacy_cannot_widen_ceiling(self):
        out = apply_tool_ceiling(
            "researcher", _defs(*_RISKS), _RISKS,
            legacy_allowed=["read_file", "write_file"])
        assert _names(out) == ["read_file"]

    def test_legacy_narrows_within_ceiling(self):
        out = apply_tool_ceiling(
            "coder", _defs(*_RISKS), _RISKS, legacy_allowed=["read_file"])
        assert _names(out) == ["read_file"]

    def test_legacy_alone_keeps_pre_p2c_behaviour(self):
        out = apply_tool_ceiling(
            "no_such_agent", _defs(*_RISKS), _RISKS,
            legacy_allowed=["read_file", "delete_file"])
        assert _names(out) == ["read_file", "delete_file"]


class TestAlreadyUsedHandling:
    def test_used_excluded_tool_kept_disabled_not_removed(self):
        out = apply_tool_ceiling(
            "researcher", _defs(*_RISKS), _RISKS,
            used_names={"write_file"})
        by_name = {d["function"]["name"]: d for d in out}
        assert by_name["write_file"]["function"]["description"] == \
            DISABLED_DESCRIPTION
        # Never used + excluded: gone outright.
        assert "delete_file" not in by_name
        assert "execute_shell" not in by_name

    def test_input_definitions_not_mutated(self):
        defs = _defs(*_RISKS)
        apply_tool_ceiling("researcher", defs, _RISKS,
                           used_names={"write_file"})
        write = next(d for d in defs
                     if d["function"]["name"] == "write_file")
        assert write["function"]["description"] == "write_file desc"


# ── Through the specialist runner ────────────────────────────────────


class CapturingProvider:
    """Streams fixed text (or one tool call); records the tools kwarg."""

    def __init__(self, text="done", tool_call=None):
        self.text = text
        self.tool_call = tool_call
        self.seen_tools = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_tools = tools
        if self.tool_call is not None:
            yield SimpleNamespace(
                text="", tool_calls=[self.tool_call],
                finish_reason="tool_calls")
        else:
            yield SimpleNamespace(
                text=self.text, tool_calls=None, finish_reason="stop")


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
        "task_id": "p2c-test",
        "session_id": "p2c-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }
    state.update(overrides)
    return state


def _config(provider, **extra):
    configurable = {"provider": provider,
                    "tool_registry": build_tool_registry()}
    configurable.update(extra)
    return {"configurable": configurable}


@pytest.fixture(autouse=True)
def _no_artifacts(monkeypatch):
    for module in (researcher_module, coder_module):
        monkeypatch.setattr(
            module, "save_artifact",
            lambda task_id, kind, filename, content: f"/tmp/{filename}")


class TestRunnerIntegration:
    @pytest.mark.asyncio
    async def test_researcher_offer_through_node(self):
        provider = CapturingProvider()
        result = await researcher_module.researcher_node(
            _state(), _config(provider))
        offered = _names(provider.seen_tools)
        assert "read_file" in offered and "search_web" in offered
        for dangerous in ("write_file", "edit_file", "delete_file",
                          "execute_shell"):
            assert dangerous not in offered
        # Offered-vs-called: one entry, this round called nothing.
        (entry,) = result["delegation_log"]
        assert entry["agent"] == "researcher"
        assert entry["tools_offered"] == offered
        assert entry["tools_called"] == []
        assert "decision" not in entry  # invisible to decision readers

    @pytest.mark.asyncio
    async def test_coder_offer_keeps_full_working_set(self):
        provider = CapturingProvider()
        await coder_module.coder_node(_state(), _config(provider))
        offered = _names(provider.seen_tools)
        for needed in ("read_file", "write_file", "edit_file",
                       "delete_file", "execute_shell"):
            assert needed in offered

    @pytest.mark.asyncio
    async def test_tool_call_round_logs_offered_and_called(self):
        call = SimpleNamespace(call_id="c1", name="search_web", args={})
        provider = CapturingProvider(tool_call=call)
        result = await researcher_module.researcher_node(
            _state(), _config(provider))
        assert result["pending_tool_call"]["name"] == "search_web"
        (entry,) = result["delegation_log"]
        assert entry["tools_called"] == ["search_web"]
        assert entry["tools_offered"] == _names(provider.seen_tools)

    @pytest.mark.asyncio
    async def test_legacy_agent_tools_intersect_through_node(self):
        provider = CapturingProvider()
        await researcher_module.researcher_node(
            _state(),
            _config(provider,
                    agent_tools={"researcher": ["read_file", "write_file"]}))
        # write_file is in the legacy list but above the ceiling.
        # (spawn_subagent rides along separately — it is appended
        # after the ceiling point, gated by the S13 settings, and is
        # not a registry tool the ceiling judges.)
        registry_offered = [n for n in _names(provider.seen_tools)
                            if n != "spawn_subagent"]
        assert registry_offered == ["read_file"]
