"""E5-F04 — accepted keyless custom providers must satisfy startup
and task-factory requirements.

ADR 0013 makes self-hosted OpenAI-compatible servers first-class, and
PROVIDER_SAVE accepts them with no secret: base URL + model catalog
is a complete configuration, and both provider-list builders mark the
card configured (``has_key`` is true for openai_compatible customs).
But the rest of the engine derived "configured" from secrets alone:

  * ``validate_startup_configuration`` built ``configured_providers``
    solely from ``secret_store.list_providers()`` — a saved keyless
    custom provider came back as ``configured_providers=[]``;
  * the START handler re-derived ``engine_state.configured_providers``
    the same secrets-only way, so routing never saw the provider;
  * ``_create_provider_instance`` gave only Ollama a no-secret
    exception — a keyless custom provider raised
    ``ProviderNotFoundError``, which START then reported to the user
    as "API key not found", demanding a secret the server does not
    have.

These tests drive all three seams with a real (empty) SecretStore,
plus controls pinning that key-requiring providers are NOT weakened:
an unkeyed built-in cloud provider still refuses, Ollama's keyless
path is unchanged, a keyed custom provider uses its real key, and a
custom provider whose saved shape is incomplete (no base URL, or an
anthropic flavour — E5-F05's territory) still raises.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.orchestration.core import _create_provider_instance
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.routing.startup import validate_startup_configuration
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore

CUSTOM = "custom_local"
CUSTOM_MODEL = "llama3:8b"
CUSTOM_URL = "http://127.0.0.1:8080/v1"


class DummyProtector(BaseProtector):
    """The test_secret_store.py seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def empty_store(tmp_path):
    return SecretStore(db_path=tmp_path / "secrets.db", protector=DummyProtector())


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="ollama", id="local-main", label="Local Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider=CUSTOM, id=CUSTOM_MODEL, label="Llama 3 8B (local)",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="custom_anthropic", id="claude-x", label="Claude X",
                   capabilities=["chat"], priority=1),
    ])


def _keyless_cfg(**overrides) -> UserConfig:
    base = {
        "default_provider": CUSTOM,
        "default_model": CUSTOM_MODEL,
        "custom_base_urls": {CUSTOM: CUSTOM_URL},
        "provider_models": {CUSTOM: [CUSTOM_MODEL]},
        "custom_provider_types": {CUSTOM: "openai_compatible"},
    }
    base.update(overrides)
    return UserConfig(**base)


# ---------------------------------------------------------------------------
# Seam (a): startup validation
# ---------------------------------------------------------------------------

class TestStartupConfiguredDerivation:
    def test_keyless_custom_is_configured_at_startup(self, empty_store):
        result = validate_startup_configuration(
            user_config=_keyless_cfg(),
            secret_store=empty_store,
            health_manager=ProviderHealthManager(),
            registry=_registry(),
        )
        assert CUSTOM in result.configured_providers

    def test_keyless_custom_default_becomes_active_without_fallback(self, empty_store):
        result = validate_startup_configuration(
            user_config=_keyless_cfg(),
            secret_store=empty_store,
            health_manager=ProviderHealthManager(),
            registry=_registry(),
        )
        assert result.configured_providers == [CUSTOM]
        assert result.active_provider == CUSTOM
        assert result.active_model == CUSTOM_MODEL
        assert result.was_fallback is False

    def test_unkeyed_builtin_cloud_still_unconfigured(self, empty_store):
        """Control: the secret requirement for real cloud providers is
        NOT weakened — no key, no configured entry."""
        cfg = UserConfig(default_provider="openai", default_model="gpt-main")
        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=empty_store,
            health_manager=ProviderHealthManager(),
            registry=_registry(),
        )
        assert "openai" not in result.configured_providers
        assert result.configured_providers == []

    def test_keyed_provider_still_configured(self, empty_store):
        """Control: the classic secret path is unchanged."""
        empty_store.save_provider("google_gemini", "gemini-key")
        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-main")
        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=empty_store,
            health_manager=ProviderHealthManager(),
            registry=_registry(),
        )
        assert result.configured_providers == ["google_gemini"]
        assert result.was_fallback is False


# ---------------------------------------------------------------------------
# Seam (b): the provider factory
# ---------------------------------------------------------------------------

@pytest.fixture
def factory_env(monkeypatch):
    cfg = _keyless_cfg()
    monkeypatch.setattr(engine_state, "user_config", cfg)
    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(engine_state, "configured_providers", [CUSTOM])
    return cfg


class TestFactoryKeylessConstruction:
    def test_factory_builds_keyless_custom(self, factory_env, empty_store):
        inst = _create_provider_instance(
            CUSTOM, CUSTOM_MODEL, empty_store, user_config=factory_env)
        assert isinstance(inst, LiteLLMProvider)
        assert inst.provider_name == CUSTOM
        assert inst.base_url == CUSTOM_URL
        # Built with the no-key placeholder, exactly like Ollama —
        # not with an invented or empty secret.
        assert inst.api_key == "sk-no-key-required"

    def test_factory_builtin_without_key_still_raises(self, factory_env, empty_store):
        with pytest.raises(ProviderNotFoundError):
            _create_provider_instance(
                "openai", "gpt-main", empty_store, user_config=factory_env)

    def test_factory_ollama_keyless_still_works(self, factory_env, empty_store):
        inst = _create_provider_instance(
            "ollama", "local-main", empty_store, user_config=factory_env)
        assert inst.provider_name == "ollama"
        assert inst.api_key == "sk-no-key-required"

    def test_factory_keyed_custom_uses_real_key(self, factory_env, empty_store):
        empty_store.save_provider(CUSTOM, "real-custom-key")
        inst = _create_provider_instance(
            CUSTOM, CUSTOM_MODEL, empty_store, user_config=factory_env)
        assert inst.api_key == "real-custom-key"
        assert inst.base_url == CUSTOM_URL

    def test_factory_custom_without_base_url_still_raises(self, factory_env, empty_store):
        """Shape-driven, not name-driven: a custom provider with no
        saved base URL has an INCOMPLETE configuration — keylessness
        must not be granted on the name alone."""
        cfg = _keyless_cfg(custom_base_urls={})
        with pytest.raises(ProviderNotFoundError):
            _create_provider_instance(
                CUSTOM, CUSTOM_MODEL, empty_store, user_config=cfg)

    def test_factory_fallback_candidate_can_be_keyless_custom(
            self, factory_env, empty_store, monkeypatch):
        """Interaction: once the keyless custom provider is in the
        configured set, it can be SELECTED as another provider's
        fallback candidate — the factory's per-candidate key load
        must resolve it to the placeholder too, not raise and drop
        the whole fallback list."""
        empty_store.save_provider("google_gemini", "gemini-key")
        monkeypatch.setattr(
            engine_state, "configured_providers", ["google_gemini", CUSTOM])
        cfg = _keyless_cfg(
            default_provider="google_gemini", default_model="gemini-main",
            fallback_chain=[CUSTOM])
        inst = _create_provider_instance(
            "google_gemini", "gemini-main", empty_store, user_config=cfg)
        fb = [fm for fm in inst.fallback_models if fm.provider_name == CUSTOM]
        assert len(fb) == 1
        assert fb[0].api_key == "sk-no-key-required"
        assert fb[0].base_url == CUSTOM_URL

    def test_factory_anthropic_flavor_custom_still_raises(self, factory_env, empty_store):
        """Boundary (E5-F05): only the openai_compatible flavour is
        keyless-capable here; an anthropic-flavour custom provider
        still requires its key in this slice."""
        cfg = _keyless_cfg(
            custom_base_urls={"custom_anthropic": "http://127.0.0.1:9090"},
            custom_provider_types={"custom_anthropic": "anthropic"},
        )
        with pytest.raises(ProviderNotFoundError):
            _create_provider_instance(
                "custom_anthropic", "claude-x", empty_store, user_config=cfg)


# ---------------------------------------------------------------------------
# Seam (c): the START handler — derivation + error surface, end to end
# over the real websocket with the real factory and a real empty store.
# The graph run itself is a scripted stub (house pattern, cf. E5-F02).
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Global config via a tmp CONFIG_PATH file (the E5-F02 lesson:
    initialize() reloads user_config from disk, so the keyless custom
    configuration must live in the config FILE). Audit log path is set
    BEFORE initialize_auth() per the fixture law."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = _keyless_cfg(workspace_roots=[str(tmp_path)])
    save_config(cfg, config_path)
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ws_module, "classify_intent", AsyncMock(return_value=None))
    registry = _registry()
    monkeypatch.setattr(engine_state, "model_registry", registry)
    monkeypatch.setattr(
        engine_state, "routing_policy", RoutingPolicy(registry=registry))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    return {"tmp_path": tmp_path, "config": cfg}


def _fake_graph(calls):
    def fake_run_langgraph_task(*args, **kwargs):
        calls.append(kwargs)

        async def gen():
            task_id = kwargs.get("task_id", "task-e5f04")
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "state": TaskState.SUCCEEDED.value,
                         "response": "answer from the local server",
                         "latency_ms": 1.0},
            )

        return gen()

    return fake_run_langgraph_task


def _receive_until_terminal(ws, max_events=30):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
            return events
    raise AssertionError(
        f"no terminal event received; got {[ev.type for ev in events]}")


class TestStartKeylessCustom:
    def test_start_routes_to_keyless_custom_and_completes(self, ws_env, empty_store):
        calls = []
        stack = ExitStack()
        # The handler builds its SecretStore via the module global;
        # hand it the REAL (empty) tmp store so the keyless path is
        # exercised honestly — no mock keys anywhere.
        stack.enter_context(patch.object(
            ws_module, "SecretStore", lambda *a, **k: empty_store))
        stack.enter_context(patch.object(
            ws_module, "run_langgraph_task", _fake_graph(calls)))
        sid = session_store.create_session("E5-F04 keyless custom")
        with (
            stack,
            TestClient(app) as client,
            client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {engine_state.auth_token}"},
            ) as ws,
        ):
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.START_TASK,
                payload={"prompt": "Please summarise the plan for today",
                         "session_id": sid},
            ).to_json())
            events = _receive_until_terminal(ws)

        # (c1) The START derivation itself now sees the provider.
        assert CUSTOM in engine_state.configured_providers
        # (c2) No "API key not found" dead end: the task completed,
        # built on the keyless custom provider at its saved base URL.
        assert events[-1].type == EventType.TASK_COMPLETED
        provider = calls[0]["provider"]
        assert provider.provider_name == CUSTOM
        assert provider.base_url == CUSTOM_URL
