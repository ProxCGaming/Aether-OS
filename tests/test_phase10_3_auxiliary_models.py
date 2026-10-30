"""Phase 10.3, B1: six of seven auxiliary-model settings were placebo.

Re-verified in the current code (branch phase-10-3-correctness):

- The six dropdowns were ALREADY removed from the Settings UI in Phase 9
  (commit ce99048); only Vision is rendered (SettingsPanel.jsx) and a Jest
  test in SettingsPanel.test.jsx pins that. That half of B1 does not
  reproduce — nothing to do there.
- What DID still reproduce was the contract-level placebo: the WebSocket
  handler (routes/websocket.py) accepted all seven task classes, persisted
  the six seam-less ones to config, audit-logged them, and echoed them back
  with success:true — while routing/policy.py reads only "vision".
  None of the six has an engine call site at all:
    web         — tools/registry.py _search_web returns raw DuckDuckGo
                  snippets as text; no LLM extraction call.
    compression — no summarisation/compaction LLM call anywhere; only
                  string truncation (prompt[:30], response[:200]).
    skills      — skills/manager.py matches manifests by keyword score;
                  skills are prompt text run by the main model.
    approval    — LangGraph native interrupt (langgraph/nodes/pause.py),
                  human-in-the-loop; no model call.
    mcp         — mcp/registry.py get_mcp_tools() returns []; scaffolding
                  with no client (MCP implementation is out of scope).
    title       — session titles are prompt[:30] + "..." (websocket.py);
                  no LLM call.
  Wiring a setting for a task class with no auxiliary call site would be
  meaningless, so per B1 the fix is removal at the contract: the handler now
  accepts only "vision", rejects the other six with a clear error, and
  prunes stale entries for them (e.g. from a pre-10.3 config file) so the
  echoed dict never advertises an override the engine will not honor.

These tests failed before the fix (the handler returned success:true and
stored the override) and pass after.
"""
import pytest

from aether_common.contracts import Event, EventType
from aether_engine.routes.websocket import AUXILIARY_TASK_CLASSES
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy, TaskRequirements
from aether_engine.routing.registry import ModelRegistry, ModelEntry

# The six task classes with no engine call site (see module docstring).
PLACEBO_TASKS = ["web", "compression", "skills", "approval", "mcp", "title"]


@pytest.fixture
def mock_registry():
    models = [
        ModelEntry(provider="google_gemini", id="gemini-2.0-flash", label="Gemini 2.0 Flash",
                   capabilities=["chat", "fast", "tools", "vision"], priority=1),
        ModelEntry(provider="openai", id="gpt-4o", label="GPT-4o",
                   capabilities=["chat", "tools", "vision", "code", "reasoning"], priority=2),
    ]
    return ModelRegistry(models)


class TestAcceptedTaskSet:
    def test_only_vision_has_an_engine_seam(self):
        # Single source of truth for what the handler accepts. If a task
        # class gains a real call site, add it here AND wire the routing
        # policy to consult it — never widen the accepted set alone.
        assert AUXILIARY_TASK_CLASSES == frozenset({"vision"})

    def test_placebo_tasks_are_not_in_accepted_set(self):
        for task in PLACEBO_TASKS:
            assert task not in AUXILIARY_TASK_CLASSES


class TestRoutingIgnoresPlaceboOverrides:
    def test_non_vision_overrides_do_not_change_routing(self, mock_registry):
        # A config carrying all six placebo overrides must route exactly as
        # if no overrides existed: the policy reads only "vision".
        policy = RoutingPolicy(mock_registry)
        health = ProviderHealthManager()
        kwargs = dict(
            prompt="describe this image",
            configured_providers=["google_gemini", "openai"],
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
            requirements=TaskRequirements(has_images=True),
        )
        placebo = {task: "openai:::gpt-4o" for task in PLACEBO_TASKS}
        with_placebo = policy.select_route(auxiliary_models=placebo, **kwargs)
        without = policy.select_route(auxiliary_models={}, **kwargs)
        assert (with_placebo.provider, with_placebo.model) == (without.provider, without.model)
        assert "auxiliary override" not in with_placebo.reason

    def test_vision_override_still_wins_alongside_placebo_keys(self, mock_registry):
        policy = RoutingPolicy(mock_registry)
        health = ProviderHealthManager()
        decision = policy.select_route(
            prompt="describe this image",
            configured_providers=["google_gemini", "openai"],
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
            requirements=TaskRequirements(has_images=True),
            auxiliary_models={**{t: "openai:::gpt-4o" for t in PLACEBO_TASKS},
                              "vision": "openai:::gpt-4o"},
        )
        assert decision.provider == "openai"
        assert decision.model == "gpt-4o"
        assert "auxiliary override" in decision.reason


class TestWebSocketRejectsPlaceboTasks:
    """End-to-end over the authenticated WebSocket (same fixture shape as
    tests/test_phase9_settings_wiring.py)."""

    @pytest.fixture
    def ws_client(self, tmp_path):
        from starlette.testclient import TestClient
        from aether_engine.app import app, engine_state

        token_file = tmp_path / "engine.token"
        audit_file = tmp_path / "audit.log"
        engine_state.token_path = token_file
        engine_state.audit_logger.log_path = audit_file
        engine_state.initialize_auth()

        orig_aux = dict(engine_state.user_config.auxiliary_models)
        orig_effort = engine_state.user_config.reasoning_effort
        orig_providers = list(engine_state.configured_providers)
        engine_state.configured_providers = ["openai", "google_gemini"]
        try:
            with TestClient(app) as client:
                live_token = engine_state.auth_token
                engine_state.configured_providers = ["openai", "google_gemini"]
                with client.websocket_connect(f"/ws/tasks?token={live_token}") as ws:
                    hello = Event.from_json(ws.receive_text())
                    assert hello.type == EventType.HELLO
                    yield ws, engine_state
        finally:
            engine_state.user_config.auxiliary_models = orig_aux
            engine_state.user_config.reasoning_effort = orig_effort
            engine_state.configured_providers = orig_providers

    def _roundtrip(self, ws, event_type, payload):
        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    @pytest.mark.parametrize("task", PLACEBO_TASKS)
    def test_setting_placebo_task_is_rejected(self, ws_client, task):
        # Before the fix this returned success:true and persisted the
        # override — a setting the engine would never honor.
        ws, engine_state = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                               {"task": task, "provider": "openai", "model": "gpt-4o"})
        assert resp.type == EventType.AUXILIARY_MODEL_SET_RESPONSE
        assert resp.payload["success"] is False
        assert task in resp.payload["error"]
        assert task not in resp.payload["auxiliary_models"]
        assert task not in engine_state.user_config.auxiliary_models

    def test_vision_still_sets_and_clears(self, ws_client):
        # Positive control: the one real seam must keep working.
        ws, engine_state = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                               {"task": "vision", "provider": "openai", "model": "gpt-4o"})
        assert resp.payload["success"] is True
        assert resp.payload["auxiliary_models"]["vision"] == "openai:::gpt-4o"
        assert engine_state.user_config.auxiliary_models["vision"] == "openai:::gpt-4o"
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                               {"task": "vision", "provider": "", "model": "auto"})
        assert resp.payload["success"] is True
        assert "vision" not in resp.payload["auxiliary_models"]

    def test_stale_placebo_entries_are_pruned_on_vision_request(self, ws_client):
        # A config file written before Phase 10.3 may still carry placebo
        # overrides. The first aux-model request must drop them (and persist
        # the cleaned dict) instead of echoing them back as live settings.
        from aether_engine.config import load_config
        from aether_engine import config as config_module

        ws, engine_state = ws_client
        for task in PLACEBO_TASKS:
            engine_state.user_config.auxiliary_models[task] = "openai:::gpt-4o"
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                               {"task": "vision", "provider": "openai", "model": "gpt-4o"})
        assert resp.payload["success"] is True
        assert resp.payload["auxiliary_models"] == {"vision": "openai:::gpt-4o"}
        assert set(engine_state.user_config.auxiliary_models) == {"vision"}
        reloaded = load_config(config_module.CONFIG_PATH)
        assert set(reloaded.auxiliary_models) == {"vision"}
