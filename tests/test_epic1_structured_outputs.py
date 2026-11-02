"""Epic 1, E1-10: structured outputs per specialist.

With an output schema configured for an agent, its final answer is
parsed and validated before the node returns; the parsed value lands
in the structured_outputs state channel. A malformed answer gets one
corrective retry carrying the validation errors; a still-malformed
retry fails loudly (explicit failure text + an error entry), never a
silent pass-through. With no schema configured, nothing changes.
"""
import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.state import merge_mapping
from aether_engine.langgraph.structured import parse_and_validate, validate_value
from aether_engine.providers.base import StreamChunk

PLAN_SCHEMA = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["low", "high"]},
    },
}


# ── Validator + parsing ──────────────────────────────────────────────

def test_validator_covers_the_supported_subset():
    assert validate_value({"steps": ["a"]}, PLAN_SCHEMA) == []
    assert any("missing required" in e for e in validate_value({}, PLAN_SCHEMA))
    assert any("expected array" in e for e in validate_value({"steps": "nope"}, PLAN_SCHEMA))
    assert any("expected string" in e for e in validate_value({"steps": [1]}, PLAN_SCHEMA))
    assert any("not one of" in e for e in validate_value({"steps": [], "risk": "mid"}, PLAN_SCHEMA))
    assert validate_value(3, {"type": "integer"}) == []
    assert validate_value(3.5, {"type": "integer"}) != []
    assert validate_value(True, {"type": "number"}) != []  # bool is not a number


def test_parse_and_validate_tolerates_fences_and_reports_bad_json():
    value, errors = parse_and_validate('```json\n{"steps": ["a"]}\n```', PLAN_SCHEMA)
    assert errors == [] and value == {"steps": ["a"]}
    value, errors = parse_and_validate("not json at all", PLAN_SCHEMA)
    assert value is None and any("not valid JSON" in e for e in errors)


def test_structured_outputs_channel_merges_per_agent():
    merged = merge_mapping({"planner": {"steps": []}}, {"coder": {"files": []}})
    assert merged == {"planner": {"steps": []}, "coder": {"files": []}}
    assert merge_mapping(None, {"a": 1}) == {"a": 1}


def test_output_schemas_round_trip_through_config():
    cfg = UserConfig(agent_output_schemas={"planner": PLAN_SCHEMA})
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.agent_output_schemas == {"planner": PLAN_SCHEMA}


# ── Node behaviour ───────────────────────────────────────────────────

class _ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        text = self.replies.pop(0) if self.replies else ""
        yield StreamChunk(text=text, finish_reason="stop")


def _planner_config(provider, user_config):
    return {"configurable": {
        "provider": provider,
        "tool_registry": None,
        "agent_models": {},
        "agent_tools": {},
        "user_config": user_config,
    }}


@pytest.fixture(autouse=True)
def _no_artifacts(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "structured/plan.txt")


def _state():
    return {"messages": [{"role": "user", "content": "plan the thing"}],
            "original_prompt": "plan the thing", "task_id": "structured-test"}


@pytest.mark.asyncio
async def test_valid_output_is_parsed_into_state():
    provider = _ScriptedProvider(['{"steps": ["one", "two"], "risk": "low"}'])
    cfg = _planner_config(provider, UserConfig(agent_output_schemas={"planner": PLAN_SCHEMA}))
    result = await planner_node(_state(), cfg)
    assert result["structured_outputs"] == {"planner": {"steps": ["one", "two"], "risk": "low"}}
    assert len(provider.calls) == 1  # no retry needed


@pytest.mark.asyncio
async def test_malformed_output_gets_one_corrective_retry():
    provider = _ScriptedProvider(["here is your plan, trust me", '{"steps": ["fixed"]}'])
    cfg = _planner_config(provider, UserConfig(agent_output_schemas={"planner": PLAN_SCHEMA}))
    result = await planner_node(_state(), cfg)
    assert result["structured_outputs"] == {"planner": {"steps": ["fixed"]}}
    assert len(provider.calls) == 2
    retry_text = " ".join(m.get("content", "") for m in provider.calls[1])
    assert "not valid structured output" in retry_text


@pytest.mark.asyncio
async def test_still_malformed_after_retry_fails_loudly():
    provider = _ScriptedProvider(["nope", "still nope"])
    cfg = _planner_config(provider, UserConfig(agent_output_schemas={"planner": PLAN_SCHEMA}))
    result = await planner_node(_state(), cfg)
    assert "Structured output validation failed for planner" in result["messages"][0]["content"]
    assert "error" in result["structured_outputs"]["planner"]
    assert len(provider.calls) == 2  # exactly one retry, no loop


@pytest.mark.asyncio
async def test_no_schema_configured_is_a_passthrough():
    provider = _ScriptedProvider(["free text plan"])
    cfg = _planner_config(provider, UserConfig())
    result = await planner_node(_state(), cfg)
    assert result["messages"][0]["content"] == "free text plan"
    assert "structured_outputs" not in result
    assert len(provider.calls) == 1
