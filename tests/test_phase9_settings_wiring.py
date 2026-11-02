"""Phase 9, B1/B2: auxiliary-model overrides and reasoning effort are real.

Before the fix, the Settings UI sent AUXILIARY_MODEL_SET_REQUEST (handled by
nothing) and stored reasoning effort in localStorage only. These tests pin the
new behavior: contract events exist, config persists, routing honors the
vision override, and the provider forwards reasoning effort.
"""
import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy, TaskRequirements
from aether_engine.routing.registry import ModelEntry, ModelRegistry


@pytest.fixture
def mock_registry():
    models = [
        ModelEntry(provider="google_gemini", id="gemini-2.0-flash", label="Gemini 2.0 Flash",
                   capabilities=["chat", "fast", "tools", "vision"], priority=1),
        ModelEntry(provider="openai", id="gpt-4o", label="GPT-4o",
                   capabilities=["chat", "tools", "vision", "code", "reasoning"], priority=2),
    ]
    return ModelRegistry(models)


class TestAuxiliaryModelContract:
    def test_contract_events_exist(self):
        assert EventType.AUXILIARY_MODEL_SET_REQUEST.value == "AUXILIARY_MODEL_SET_REQUEST"
        assert EventType.AUXILIARY_MODEL_SET_RESPONSE.value == "AUXILIARY_MODEL_SET_RESPONSE"
        assert EventType.REASONING_EFFORT_SET_REQUEST.value == "REASONING_EFFORT_SET_REQUEST"
        assert EventType.REASONING_EFFORT_SET_RESPONSE.value == "REASONING_EFFORT_SET_RESPONSE"

    def test_config_round_trip(self):
        cfg = UserConfig()
        cfg.auxiliary_models = {"vision": "openai:::gpt-4o", "title": "google_gemini:::gemini-2.0-flash"}
        cfg.reasoning_effort = "high"
        restored = UserConfig.from_dict(cfg.to_dict())
        # Phase 10.3 B1 dropped seam-less overrides on load; Epic 1 (E1-08)
        # gave "title" a real engine seam (session_titles), so both survive.
        assert restored.auxiliary_models == {
            "vision": "openai:::gpt-4o",
            "title": "google_gemini:::gemini-2.0-flash",
        }
        assert restored.reasoning_effort == "high"

    def test_config_defaults_sane(self):
        restored = UserConfig.from_dict({})
        assert restored.auxiliary_models == {}
        assert restored.reasoning_effort == "standard"


class TestVisionOverrideRouting:
    def test_vision_override_wins_over_heuristic(self, mock_registry):
        policy = RoutingPolicy(mock_registry)
        health = ProviderHealthManager()
        decision = policy.select_route(
            prompt="describe this image",
            configured_providers=["google_gemini", "openai"],
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
            requirements=TaskRequirements(has_images=True),
            auxiliary_models={"vision": "openai:::gpt-4o"},
        )
        assert decision.provider == "openai"
        assert decision.model == "gpt-4o"
        assert "auxiliary override" in decision.reason

    def test_no_override_uses_default_vision_model(self, mock_registry):
        policy = RoutingPolicy(mock_registry)
        health = ProviderHealthManager()
        decision = policy.select_route(
            prompt="describe this image",
            configured_providers=["google_gemini", "openai"],
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
            requirements=TaskRequirements(has_images=True),
            auxiliary_models={},
        )
        # Default model has vision capability -> heuristic keeps it.
        assert decision.provider == "google_gemini"
        assert "auxiliary override" not in decision.reason

    def test_override_to_unconfigured_provider_ignored(self, mock_registry):
        policy = RoutingPolicy(mock_registry)
        health = ProviderHealthManager()
        decision = policy.select_route(
            prompt="describe this image",
            configured_providers=["google_gemini"],  # openai not configured
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-2.0-flash",
            requirements=TaskRequirements(has_images=True),
            auxiliary_models={"vision": "openai:::gpt-4o"},
        )
        assert decision.provider == "google_gemini"


class TestReasoningEffortForwarding:
    def test_effort_forwarded_when_non_default(self):
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key", model="gpt-4o", provider_name="openai", reasoning_effort="high"
        )
        kwargs = provider._build_call_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["reasoning_effort"] == "high"

    def test_effort_omitted_when_default(self):
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key", model="gpt-4o", provider_name="openai", reasoning_effort="standard"
        )
        kwargs = provider._build_call_kwargs([{"role": "user", "content": "hi"}])
        assert "reasoning_effort" not in kwargs


class TestSettingsWebSocketHandlers:
    """End-to-end over the authenticated WebSocket: the UI's messages must be
    handled, validated, and persisted — not silently dropped (Phase 9, B1/B2).
    """

    @pytest.fixture
    def ws_client(self, tmp_path):
        from starlette.testclient import TestClient

        from aether_engine.app import app, engine_state

        token_file = tmp_path / "engine.token"
        audit_file = tmp_path / "audit.log"
        engine_state.token_path = token_file
        engine_state.audit_logger.log_path = audit_file
        engine_state.initialize_auth()

        # initialize() replaces user_config and recomputes configured_providers,
        # so snapshot/mutate only after it runs.
        orig_aux = dict(engine_state.user_config.auxiliary_models)
        orig_effort = engine_state.user_config.reasoning_effort
        orig_providers = list(engine_state.configured_providers)
        engine_state.configured_providers = ["openai", "google_gemini"]
        try:
            with TestClient(app) as client:
                # The app lifespan re-runs initialize() on entry, rotating the
                # token — read the live one instead of the pre-entry value.
                # It also recomputes configured_providers, so set ours after.
                live_token = engine_state.auth_token
                engine_state.configured_providers = ["openai", "google_gemini"]
                with client.websocket_connect(f"/ws/tasks?token={live_token}") as ws:
                    from aether_common.contracts import Event
                    hello = Event.from_json(ws.receive_text())
                    assert hello.type == EventType.HELLO
                    yield ws, engine_state
        finally:
            engine_state.user_config.auxiliary_models = orig_aux
            engine_state.user_config.reasoning_effort = orig_effort
            engine_state.configured_providers = orig_providers

    def _roundtrip(self, ws, event_type, payload):
        from aether_common.contracts import Event
        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_auxiliary_model_set_roundtrip(self, ws_client):
        from aether_engine import config as config_module
        from aether_engine.config import load_config

        ws, engine_state = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "vision", "provider": "openai", "model": "gpt-4o"})
        assert resp.type == EventType.AUXILIARY_MODEL_SET_RESPONSE
        assert resp.payload["success"] is True
        assert resp.payload["auxiliary_models"]["vision"] == "openai:::gpt-4o"
        assert engine_state.user_config.auxiliary_models["vision"] == "openai:::gpt-4o"

        # Persisted to disk: reload the config file and confirm.
        reloaded = load_config(config_module.CONFIG_PATH)
        assert reloaded.auxiliary_models.get("vision") == "openai:::gpt-4o"

    def test_auxiliary_model_set_rejects_unknown_task(self, ws_client):
        ws, _ = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "teleport", "provider": "openai", "model": "gpt-4o"})
        assert resp.payload["success"] is False
        assert "teleport" in resp.payload["error"]

    def test_auxiliary_model_set_rejects_unconfigured_provider(self, ws_client):
        ws, _ = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "vision", "provider": "nope", "model": "x"})
        assert resp.payload["success"] is False

    def test_auxiliary_model_auto_clears_override(self, ws_client):
        ws, _engine_state = ws_client
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "vision", "provider": "openai", "model": "gpt-4o"})
        assert resp.payload["success"] is True
        assert resp.payload["auxiliary_models"]["vision"] == "openai:::gpt-4o"
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "vision", "provider": "", "model": "auto"})
        assert resp.payload["success"] is True
        assert "vision" not in resp.payload["auxiliary_models"]

    def test_auxiliary_model_rejects_task_with_no_engine_seam(self, ws_client):
        # Phase 10.3, B1: helper tasks with no engine call site must be
        # rejected, not accepted and then silently ignored, and a stale
        # entry for one is pruned so the echoed dict never advertises an
        # override the engine will not honor. Epic 1 gave compression,
        # advisor and title real seams; "web" remains seam-less and is the
        # probe here.
        ws, engine_state = ws_client
        engine_state.user_config.auxiliary_models["web"] = "openai:::gpt-4o"
        resp = self._roundtrip(ws, EventType.AUXILIARY_MODEL_SET_REQUEST,
                              {"task": "web", "provider": "", "model": "auto"})
        assert resp.payload["success"] is False
        assert "web" in resp.payload["error"]
        assert "web" not in resp.payload["auxiliary_models"]
        assert "web" not in engine_state.user_config.auxiliary_models

    def test_reasoning_effort_set_roundtrip(self, ws_client):
        from aether_engine import config as config_module
        from aether_engine.config import load_config

        ws, engine_state = ws_client
        resp = self._roundtrip(ws, EventType.REASONING_EFFORT_SET_REQUEST, {"effort": "high"})
        assert resp.type == EventType.REASONING_EFFORT_SET_RESPONSE
        assert resp.payload["success"] is True
        assert resp.payload["effort"] == "high"
        assert engine_state.user_config.reasoning_effort == "high"
        assert load_config(config_module.CONFIG_PATH).reasoning_effort == "high"

    def test_reasoning_effort_rejects_invalid(self, ws_client):
        ws, _ = ws_client
        resp = self._roundtrip(ws, EventType.REASONING_EFFORT_SET_REQUEST, {"effort": "ultra"})
        assert resp.payload["success"] is False
