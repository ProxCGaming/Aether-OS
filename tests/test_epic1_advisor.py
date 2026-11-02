"""Epic 1, E1-12: the advisor pass.

With auxiliary_models["advisor"] configured, each specialist's system
prompt gains independent review notes from the advisor model. Without
a usable spec there is no advisor call and zero behaviour change, and
an advisor failure degrades to no notes — never a failed task.
"""
import pytest

from aether_engine.config import AUXILIARY_TASK_CLASSES, UserConfig
from aether_engine.langgraph import advisor as advisor_module
from aether_engine.langgraph.advisor import advisor_notes, resolve_advisor_provider
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.providers.base import StreamChunk

NOTES = "- Check the rollback path before editing."


class _AdvisorProvider:
    def __init__(self, text=NOTES, fail=False):
        self.text = text
        self.fail = fail
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "node": node_name})
        if self.fail:
            raise RuntimeError("advisor is down")
        yield StreamChunk(text=self.text, finish_reason="stop")


class _MainProvider:
    def __init__(self):
        self.systems = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.systems.append(messages[0]["content"] if messages else "")
        yield StreamChunk(text="planned", finish_reason="stop")


def _config(user_config, secret_store="store"):
    return {"configurable": {"user_config": user_config, "secret_store": secret_store}}


def _advisor_config():
    return UserConfig(auxiliary_models={"advisor": "google_gemini:::gemini-3.5-flash"})


def test_advisor_is_a_real_auxiliary_task_class():
    assert "advisor" in AUXILIARY_TASK_CLASSES
    cfg = UserConfig.from_dict({"auxiliary_models": {"advisor": "google_gemini:::gemini-3.5-flash"}})
    assert cfg.auxiliary_models.get("advisor") == "google_gemini:::gemini-3.5-flash"


def test_resolve_returns_none_without_a_usable_spec():
    assert resolve_advisor_provider(UserConfig(), "store") is None
    assert resolve_advisor_provider(UserConfig(auxiliary_models={"advisor": "auto"}), "store") is None
    assert resolve_advisor_provider(_advisor_config(), None) is None  # no secret store
    assert resolve_advisor_provider(UserConfig(auxiliary_models={"advisor": "no-separator"}), "store") is None


@pytest.mark.asyncio
async def test_notes_come_from_the_advisor_model(monkeypatch):
    advisor = _AdvisorProvider()
    monkeypatch.setattr(advisor_module, "resolve_advisor_provider", lambda uc, ss: advisor)
    notes = await advisor_notes(_config(_advisor_config()),
                                {"original_prompt": "migrate the db", "messages": []}, "planner")
    assert notes == NOTES
    assert advisor.calls[0]["node"] == "advisor"
    assert "migrate the db" in advisor.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_no_config_means_no_call_and_no_notes(monkeypatch):
    called = []
    monkeypatch.setattr(advisor_module, "resolve_advisor_provider",
                        lambda uc, ss: called.append(1) or None)
    notes = await advisor_notes(_config(UserConfig()), {"original_prompt": "x", "messages": []}, "coder")
    assert notes == ""


@pytest.mark.asyncio
async def test_advisor_failure_degrades_to_no_notes(monkeypatch):
    monkeypatch.setattr(advisor_module, "resolve_advisor_provider",
                        lambda uc, ss: _AdvisorProvider(fail=True))
    notes = await advisor_notes(_config(_advisor_config()),
                                {"original_prompt": "x", "messages": []}, "researcher")
    assert notes == ""


@pytest.fixture(autouse=True)
def _no_artifacts(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "advisor/plan.txt")


def _planner_config(main_provider, user_config):
    return {"configurable": {
        "provider": main_provider,
        "tool_registry": None,
        "agent_models": {},
        "agent_tools": {},
        "user_config": user_config,
        "secret_store": "store",
    }}


@pytest.mark.asyncio
async def test_planner_prompt_carries_notes_only_when_configured(monkeypatch):
    monkeypatch.setattr(
        advisor_module, "resolve_advisor_provider",
        lambda uc, ss: _AdvisorProvider() if (getattr(uc, "auxiliary_models", None) or {}).get("advisor") else None)
    state = {"messages": [{"role": "user", "content": "plan it"}],
             "original_prompt": "plan it", "task_id": "advisor-test"}

    main = _MainProvider()
    await planner_node(state, _planner_config(main, _advisor_config()))
    assert "Advisor review notes" in main.systems[-1]
    assert "rollback path" in main.systems[-1]

    main2 = _MainProvider()
    await planner_node(state, _planner_config(main2, UserConfig()))
    assert "Advisor review notes" not in main2.systems[-1]
