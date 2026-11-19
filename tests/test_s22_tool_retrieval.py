"""S22 contract tests — P2c step 3 (orchestration programme, slice
K-03): Filter 2 task-based tool retrieval.

The contract under test:

* Retrieval runs INSIDE Filter 1's output (tools/ceilings.py) and
  only ever narrows it — the offered set after Filter 2 is a subset
  of the ceiling set, in the ceiling set's order.
* `UserConfig.tool_retrieval` defaults to "off": offers are then
  exactly today's (the whole ceiling set). "auto" narrows per
  delegation to the briefing's top-K (default 10, range 8–12) plus
  the agent's pinned essentials.
* Confidence fallback: a set that already fits in K, or a briefing
  with no retrieval signal, is offered whole — retrieval never
  starves an agent.
* The delegation-log shape from step 2 is unchanged: entries carry
  exactly {agent, timestamp, tools_offered, tools_called}, no
  `decision` key, and `tools_offered` is the retrieved set the model
  was actually offered.
* Filter 3 (policy/approval at call time) is untouched and not
  re-tested here; `discover_tools` (step 4) is data-gated and is
  deliberately NOT built by this slice.
"""
from types import SimpleNamespace

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.tools.ceilings import apply_tool_ceiling
from aether_engine.tools.registry import ToolRisk, build_tool_registry
from aether_engine.tools.retrieval import (
    ToolIndex,
    apply_tool_retrieval,
    pinned_for,
    tool_retrieval_mode,
    tool_retrieval_top_k,
)


def _defs(*specs):
    """specs: (name, description) pairs."""
    return [
        {"type": "function",
         "function": {"name": n, "description": d, "parameters": {}}}
        for n, d in specs
    ]


def _names(definitions):
    return [d["function"]["name"] for d in definitions]


# A 14-tool synthetic pool: big enough that K=8 narrows it, with
# descriptions written so the relevant ranking is unambiguous.
_POOL = _defs(
    ("search_web", "Search the web for information and recent papers."),
    ("fetch_url", "Fetch one web page by URL and return its text."),
    ("read_file", "Read a text file inside the workspace."),
    ("find_files", "Find files by glob pattern inside the workspace."),
    ("search_text", "Search file contents with a regular expression."),
    ("write_file", "Create or overwrite a file on disk with content."),
    ("edit_file", "Edit an existing file with anchored replacements."),
    ("delete_file", "Delete a file from disk if it exists."),
    ("execute_shell", "Run a local shell command."),
    ("run_checks", "Run the repository checks: pytest and lint."),
    ("list_events", "List upcoming calendar events and meetings."),
    ("add_event", "Add an event to the calendar schedule."),
    ("get_time", "Get the current local date and time."),
    ("media_recall", "Find photos and videos from earlier chats."),
)
_RISKS = {
    "search_web": ToolRisk.READ_ONLY, "fetch_url": ToolRisk.READ_ONLY,
    "read_file": ToolRisk.READ_ONLY, "find_files": ToolRisk.READ_ONLY,
    "search_text": ToolRisk.READ_ONLY, "write_file": ToolRisk.WRITE,
    "edit_file": ToolRisk.WRITE, "delete_file": ToolRisk.DESTRUCTIVE,
    "execute_shell": ToolRisk.EXECUTE, "run_checks": ToolRisk.EXECUTE,
    "list_events": ToolRisk.READ_ONLY, "add_event": ToolRisk.WRITE,
    "get_time": ToolRisk.READ_ONLY, "media_recall": ToolRisk.READ_ONLY,
}


class TestIndex:
    def test_web_query_ranks_web_tools_first(self):
        index = ToolIndex.from_definitions(_POOL, _RISKS)
        scored = index.search("search the web for recent papers",
                              _names(_POOL))
        top = [name for name, score in scored if score > 0.0]
        assert top[0] == "search_web"
        assert "fetch_url" in top[:3]
        assert "delete_file" not in top

    def test_search_is_deterministic(self):
        index = ToolIndex.from_definitions(_POOL, _RISKS)
        first = index.search("edit the file and run checks", _names(_POOL))
        second = index.search("edit the file and run checks", _names(_POOL))
        assert first == second

    def test_no_shared_vocabulary_scores_zero(self):
        index = ToolIndex.from_definitions(_POOL, _RISKS)
        scored = index.search("zxqw vkjh plmokn", _names(_POOL))
        assert all(score == 0.0 for _, score in scored)


class TestRetrieval:
    def test_top_k_narrows_and_keeps_relevant(self):
        out = apply_tool_retrieval(
            "coder", _POOL, _RISKS,
            query="edit the handler file and run the checks",
            top_k=8)
        offered = _names(out)
        assert len(offered) < len(_POOL)
        assert "edit_file" in offered and "run_checks" in offered

    def test_output_is_subset_in_input_order(self):
        out = apply_tool_retrieval(
            "coder", _POOL, _RISKS,
            query="search the web for papers", top_k=8)
        offered = _names(out)
        assert set(offered) <= set(_names(_POOL))
        assert offered == [n for n in _names(_POOL) if n in set(offered)]

    def test_small_set_offered_whole(self):
        small = _POOL[:6]
        out = apply_tool_retrieval(
            "coder", small, _RISKS, query="anything at all", top_k=8)
        assert _names(out) == _names(small)

    def test_no_signal_falls_back_to_full_set(self):
        out = apply_tool_retrieval(
            "coder", _POOL, _RISKS, query="zxqw vkjh plmokn", top_k=8)
        assert _names(out) == _names(_POOL)

    def test_pinned_essential_survives_zero_score(self):
        # A calendar brief gives the coder's pinned file tools no
        # lexical signal; the pins still ride.
        out = apply_tool_retrieval(
            "coder", _POOL, _RISKS,
            query="list the calendar events and meetings schedule",
            top_k=8)
        for pinned in pinned_for("coder"):
            assert pinned in _names(out)

    def test_used_tool_survives_zero_score(self):
        out = apply_tool_retrieval(
            "coder", _POOL, _RISKS,
            query="search the web for recent papers", top_k=8,
            used_names={"delete_file"})
        assert "delete_file" in _names(out)

    def test_input_not_mutated(self):
        snapshot = [dict(d, function=dict(d["function"])) for d in _POOL]
        apply_tool_retrieval("coder", _POOL, _RISKS,
                             query="edit the file", top_k=8)
        assert _POOL == snapshot


class TestConfigSurface:
    def test_defaults_are_off_and_ten(self):
        config = UserConfig()
        assert tool_retrieval_mode(config) == "off"
        assert tool_retrieval_top_k(config) == 10

    def test_mode_is_auto_only_when_exactly_auto(self):
        assert tool_retrieval_mode(
            SimpleNamespace(tool_retrieval="auto")) == "auto"
        assert tool_retrieval_mode(
            SimpleNamespace(tool_retrieval="AUTO")) == "off"
        assert tool_retrieval_mode(None) == "off"
        assert tool_retrieval_mode(SimpleNamespace()) == "off"

    def test_top_k_clamped_to_plan_range(self):
        assert tool_retrieval_top_k(
            SimpleNamespace(tool_retrieval_top_k=99)) == 12
        assert tool_retrieval_top_k(
            SimpleNamespace(tool_retrieval_top_k=2)) == 8
        assert tool_retrieval_top_k(
            SimpleNamespace(tool_retrieval_top_k="junk")) == 10

    def test_config_round_trip(self):
        config = UserConfig()
        config.tool_retrieval = "auto"
        config.tool_retrieval_top_k = 9
        restored = UserConfig.from_dict(config.to_dict())
        assert restored.tool_retrieval == "auto"
        assert restored.tool_retrieval_top_k == 9
        bad = UserConfig.from_dict({"tool_retrieval": "sometimes"})
        assert bad.tool_retrieval == "off"


class TestGoldenRealRegistry:
    """The plan's golden cases, against the shipped registry."""

    def _ceiling_then_retrieval(self, agent, brief, **kwargs):
        registry = build_tool_registry()
        risk_by_name = {t.name: t.risk for t in registry.get_tools()}
        ceiling_set = apply_tool_ceiling(
            agent, registry.get_definitions(), risk_by_name)
        return apply_tool_retrieval(
            agent, ceiling_set, risk_by_name, query=brief, **kwargs), \
            risk_by_name

    def test_researcher_brief_never_surfaces_write_or_execute(self):
        # The brief's own words ("build", "run") must not pull
        # write/execute tools in — Filter 1 already excluded them
        # and Filter 2 can only narrow further.
        out, risk_by_name = self._ceiling_then_retrieval(
            "researcher",
            "Survey recent papers on retrieval augmented generation; "
            "build a comparison table and run a summary of findings.")
        offered = _names(out)
        assert offered
        assert "search_web" in offered
        for name in offered:
            assert risk_by_name[name] == ToolRisk.READ_ONLY, name
        for dangerous in ("write_file", "edit_file", "delete_file",
                          "execute_shell"):
            assert dangerous not in offered

    def test_coder_brief_surfaces_workspace_write_set(self):
        out, _ = self._ceiling_then_retrieval(
            "coder",
            "Fix the failing login test: read the auth module, edit "
            "the token handler, write a regression test, run checks.")
        offered = _names(out)
        registry = build_tool_registry()
        assert len(offered) < len(registry.get_definitions())
        for needed in ("read_file", "write_file", "edit_file",
                       "execute_shell"):
            assert needed in offered


# ── Through the specialist runner (the S22 log contract) ────────────


class CapturingProvider:
    """Streams fixed text; records the tools kwarg."""

    def __init__(self, text="done"):
        self.text = text
        self.seen_tools = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_tools = tools
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
        "task_id": "s22-test",
        "session_id": "s22-session",
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


_AUTO = SimpleNamespace(tool_retrieval="auto", tool_retrieval_top_k=10)


class TestRunnerContract:
    @pytest.mark.asyncio
    async def test_off_by_default_offer_matches_ceiling_only_path(self):
        provider = CapturingProvider()
        await coder_module.coder_node(_state(), _config(provider))
        offered = [n for n in _names(provider.seen_tools)
                   if n != "spawn_subagent"]
        registry = build_tool_registry()
        risk_by_name = {t.name: t.risk for t in registry.get_tools()}
        ceiling_names = _names(apply_tool_ceiling(
            "coder", registry.get_definitions(), risk_by_name))
        # Desktop actuation is removed by its own later filter when
        # control is not permitted (the default); nothing else may
        # differ with retrieval off.
        expected = [n for n in ceiling_names if n != "desktop_act"]
        assert offered == expected

    @pytest.mark.asyncio
    async def test_auto_narrows_and_log_records_the_retrieved_set(self):
        provider = CapturingProvider()
        brief = ("Fix the failing login test: read the auth module, "
                 "edit the token handler, write a regression test, "
                 "run the checks.")
        result = await coder_module.coder_node(
            _state(original_prompt=brief,
                   messages=[{"role": "user", "content": brief}]),
            _config(provider, user_config=_AUTO))
        offered = _names(provider.seen_tools)
        registry_names = _names(build_tool_registry().get_definitions())
        assert len(offered) < len(registry_names)
        for needed in ("read_file", "write_file", "edit_file"):
            assert needed in offered
        (entry,) = result["delegation_log"]
        assert set(entry) == {"agent", "timestamp", "tools_offered",
                              "tools_called"}
        assert "decision" not in entry
        assert entry["tools_offered"] == offered

    @pytest.mark.asyncio
    async def test_auto_researcher_stays_read_only_through_node(self):
        provider = CapturingProvider()
        brief = ("Survey recent papers on retrieval augmented "
                 "generation; build a comparison and run a summary.")
        await researcher_module.researcher_node(
            _state(original_prompt=brief,
                   messages=[{"role": "user", "content": brief}]),
            _config(provider, user_config=_AUTO))
        offered = _names(provider.seen_tools)
        risk_by_name = {t.name: t.risk
                        for t in build_tool_registry().get_tools()}
        assert offered
        for name in offered:
            if name == "spawn_subagent":
                continue  # appended after retrieval; not a registry tool
            assert risk_by_name[name] == ToolRisk.READ_ONLY, name
