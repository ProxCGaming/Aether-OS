"""E5-F02 — fallback configuration must govern BOTH enforcement points
(and the effective task policy).

ADR 0033 enforces fallback at exactly two points: per-call (inside
``LiteLLMProvider.call_stream``, fed by the ``fallback_models`` that
``_create_provider_instance`` computes) and task-level (the websocket
pump's retry factory). The user's fallback configuration —
``disable_fallbacks`` and the ordered ``fallback_chain`` — was only
honoured at the per-call point, and only from the GLOBAL config:

  * ``_task_retry_factory`` took ``get_fallback_candidates(...)[0]``
    and never consulted either control — a task retried onto the
    first health-derived candidate even when fallbacks were disabled
    or a chain named a different provider;
  * ``_create_provider_instance`` accepted no effective-config
    parameter and read the globals, and START built the provider
    BEFORE resolving the task's effective config — so a per-task
    overlay disabling fallback (or naming a chain) still produced
    the global fallback list.

These tests drive both points with configurations that forbid or
redirect fallback, plus controls pinning the unchanged default/global
behaviour. All providers are scripted stubs: no network.
"""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.orchestration.core import _create_provider_instance
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.fallback import get_fallback_candidates
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

PROVIDERS = ["google_gemini", "openai", "anthropic"]


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat", "code", "tools"], priority=1),
        ModelEntry(provider="google_gemini", id="gemini-alt", label="Gemini Alt",
                   capabilities=["chat"], priority=2),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-main", label="Claude Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-vision", label="Claude Vision",
                   capabilities=["chat", "vision"], priority=2),
        ModelEntry(provider="ollama", id="local-main", label="Local Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="media_only", id="image-gen", label="Image Gen",
                   capabilities=["image"], priority=1),
    ])


def _authority():
    """The single fallback-policy authority (E5-F02). Imported lazily
    so a tree without it fails these tests individually instead of
    breaking collection for the behavioural repros below."""
    from aether_engine.routing.fallback import (
        get_configured_fallback_candidates,
    )
    return get_configured_fallback_candidates


def _cfg(**overrides) -> UserConfig:
    base = {"default_provider": "google_gemini", "default_model": "gemini-main"}
    base.update(overrides)
    return UserConfig(**base)


# ---------------------------------------------------------------------------
# The authority: one interpretation of (disable_fallbacks, fallback_chain)
# ---------------------------------------------------------------------------

class TestConfiguredFallbackCandidates:
    def test_disable_returns_no_candidates(self):
        health = ProviderHealthManager()
        result = _authority()(
            "google_gemini", _cfg(disable_fallbacks=True),
            configured_providers=PROVIDERS, health_manager=health,
            registry=_registry())
        assert result == []

    def test_chain_order_governs_not_configured_order(self):
        health = ProviderHealthManager()
        result = _authority()(
            "google_gemini", _cfg(fallback_chain=["anthropic", "openai"]),
            configured_providers=PROVIDERS, health_manager=health,
            registry=_registry())
        assert [e.provider for e in result] == ["anthropic", "openai"]

    def test_chain_skips_failed_and_unavailable_providers(self):
        health = ProviderHealthManager()
        health.record_failure("anthropic", "Auth failed", is_retriable=False)
        result = _authority()(
            "google_gemini",
            _cfg(fallback_chain=["google_gemini", "anthropic", "openai"]),
            configured_providers=PROVIDERS, health_manager=health,
            registry=_registry())
        assert [e.provider for e in result] == ["openai"]

    def test_chain_skips_providers_without_a_chat_model(self):
        health = ProviderHealthManager()
        result = _authority()(
            "google_gemini", _cfg(fallback_chain=["media_only", "openai"]),
            configured_providers=PROVIDERS, health_manager=health,
            registry=_registry())
        assert [e.provider for e in result] == ["openai"]

    def test_chain_require_vision_picks_the_vision_model(self):
        health = ProviderHealthManager()
        result = _authority()(
            "google_gemini", _cfg(fallback_chain=["anthropic"]),
            configured_providers=PROVIDERS, health_manager=health,
            registry=_registry(), require_vision=True)
        assert [e.id for e in result] == ["claude-vision"]

    def test_chain_local_only_excludes_cloud_providers(self):
        health = ProviderHealthManager()
        result = _authority()(
            "google_gemini", _cfg(fallback_chain=["openai", "ollama"]),
            configured_providers=PROVIDERS + ["ollama"],
            health_manager=health, registry=_registry(), local_only=True)
        assert [e.provider for e in result] == ["ollama"]

    def test_no_chain_delegates_to_health_derivation(self):
        health = ProviderHealthManager()
        health.record_failure("anthropic", "Auth failed", is_retriable=False)
        expected = get_fallback_candidates(
            "google_gemini", configured_providers=PROVIDERS,
            health_manager=health, registry=_registry())
        result = _authority()(
            "google_gemini", _cfg(), configured_providers=PROVIDERS,
            health_manager=health, registry=_registry())
        assert [e.id for e in result] == [e.id for e in expected]
        assert [e.provider for e in result] == ["openai"]


# ---------------------------------------------------------------------------
# Per-call enforcement point: _create_provider_instance must take the
# effective config as its fallback-policy authority.
# ---------------------------------------------------------------------------

@pytest.fixture
def factory_env(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(engine_state, "user_config", cfg)
    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(engine_state, "configured_providers", PROVIDERS)
    store = MagicMock()
    store.load_provider.return_value = "fake-key"
    return cfg, store


def _cross_provider_names(provider_inst):
    return [fm.provider_name for fm in provider_inst.fallback_models
            if fm.provider_name != "google_gemini"]


class TestFactoryFallbackGovernance:
    def test_global_disable_control(self, factory_env):
        cfg, store = factory_env
        cfg.disable_fallbacks = True
        inst = _create_provider_instance("google_gemini", "gemini-main", store)
        assert inst.fallback_models == []

    def test_global_chain_control(self, factory_env):
        cfg, store = factory_env
        cfg.fallback_chain = ["anthropic"]
        inst = _create_provider_instance("google_gemini", "gemini-main", store)
        assert _cross_provider_names(inst) == ["anthropic"]

    def test_default_derivation_control(self, factory_env):
        _, store = factory_env
        inst = _create_provider_instance("google_gemini", "gemini-main", store)
        assert _cross_provider_names(inst) == ["openai", "anthropic"]

    def test_effective_disable_overrides_global_allow(self, factory_env):
        _, store = factory_env
        effective = _cfg(disable_fallbacks=True)
        inst = _create_provider_instance(
            "google_gemini", "gemini-main", store, user_config=effective)
        assert inst.fallback_models == []

    def test_effective_chain_overrides_global_default(self, factory_env):
        _, store = factory_env
        effective = _cfg(fallback_chain=["anthropic"])
        inst = _create_provider_instance(
            "google_gemini", "gemini-main", store, user_config=effective)
        assert _cross_provider_names(inst) == ["anthropic"]


# ---------------------------------------------------------------------------
# Both points end-to-end: drive the real websocket START handler.
# The graph run is a scripted stub (house pattern, cf. F-09): first
# attempt fails retriably before any tool activity; a rebuilt attempt
# completes.
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + stores isolated to tmp (F-09 ws_env pattern):
    the audit log path is set BEFORE initialize_auth() — the
    process-global audit logger otherwise leaks a dangling path
    across suites.

    The global config is controlled through a tmp CONFIG_PATH:
    ``engine_state.initialize()`` (called here AND again by the app
    lifespan when TestClient starts) reloads ``user_config`` via
    ``load_config()``, so global fallback settings must live in the
    config FILE, not in a patched attribute. ``set_global`` rewrites
    that file; the drive's lifespan load then sees the new values."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = _cfg(workspace_roots=[str(tmp_path)])
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

    def set_global(**fields):
        for key, value in fields.items():
            setattr(cfg, key, value)
        save_config(cfg, config_path)

    return {"tmp_path": tmp_path, "config": cfg, "set_global": set_global}


def _fake_graph(calls, *, fail_first=True):
    def fake_run_langgraph_task(*args, **kwargs):
        calls.append(kwargs)

        async def gen():
            task_id = kwargs.get("task_id", "task-e5f02")
            yield Event(
                type=EventType.TASK_CREATED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "prompt": kwargs.get("prompt", ""),
                         "state": TaskState.RUNNING.value},
            )
            if fail_first and len(calls) == 1:
                yield Event(
                    type=EventType.TASK_FAILED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": task_id,
                             "error": "429 rate limited",
                             "state": TaskState.FAILED.value,
                             "retriable": True,
                             "latency_ms": 1.0},
                )
                return
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=kwargs.get("request_id"),
                payload={"task_id": task_id,
                         "state": TaskState.SUCCEEDED.value,
                         "response": "fallback answer",
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


def _drive_task(ws_env, *, overlay=None, patch_factory=True, fail_first=True):
    calls = []
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = PROVIDERS
    mock_store.load_provider.return_value = "fake-key"
    if patch_factory:
        stack.enter_context(patch.object(
            ws_module, "_create_provider_instance",
            return_value=SimpleNamespace()))
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task",
        _fake_graph(calls, fail_first=fail_first)))
    sid = session_store.create_session("E5-F02 governance")
    with (
        stack,
        TestClient(app) as client,
        client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        ) as ws,
    ):
        ws.receive_text()  # HELLO
        payload = {"prompt": "Please summarise the plan for today",
                   "session_id": sid}
        if overlay is not None:
            payload["config_overlay"] = overlay
        ws.send_text(Event(type=EventType.START_TASK, payload=payload).to_json())
        events = _receive_until_terminal(ws)
    return calls, events


def _created_providers(events):
    return [ev.payload.get("routing", {}).get("provider")
            for ev in events if ev.type == EventType.TASK_CREATED]


class TestTaskRetryGovernance:
    def test_retry_blocked_by_global_disable(self, ws_env):
        ws_env["set_global"](disable_fallbacks=True)
        calls, events = _drive_task(ws_env)
        assert events[-1].type == EventType.TASK_FAILED
        assert len(calls) == 1

    def test_retry_follows_global_chain(self, ws_env):
        ws_env["set_global"](fallback_chain=["anthropic"])
        calls, events = _drive_task(ws_env)
        assert events[-1].type == EventType.TASK_COMPLETED
        assert len(calls) == 2
        assert _created_providers(events)[-1] == "anthropic"

    def test_retry_blocked_by_overlay_disable(self, ws_env):
        calls, events = _drive_task(
            ws_env, overlay={"disable_fallbacks": True})
        assert events[-1].type == EventType.TASK_FAILED
        assert len(calls) == 1

    def test_retry_follows_overlay_chain(self, ws_env):
        calls, events = _drive_task(
            ws_env, overlay={"fallback_chain": ["anthropic"]})
        assert events[-1].type == EventType.TASK_COMPLETED
        assert len(calls) == 2
        assert _created_providers(events)[-1] == "anthropic"

    def test_retry_default_path_control(self, ws_env):
        """No disable, no chain anywhere: the retry still lands on the
        first healthy configured candidate, exactly as before."""
        calls, events = _drive_task(ws_env)
        assert events[-1].type == EventType.TASK_COMPLETED
        assert len(calls) == 2
        assert _created_providers(events)[-1] == "openai"


class TestStartPerCallGovernance:
    def test_start_provider_honours_overlay_disable(self, ws_env):
        """The REAL factory builds START's provider: an overlay that
        disables fallbacks must leave it with no fallback models,
        even though the global config allows them."""
        calls, events = _drive_task(
            ws_env, overlay={"disable_fallbacks": True},
            patch_factory=False, fail_first=False)
        assert events[-1].type == EventType.TASK_COMPLETED
        provider = calls[0]["provider"]
        assert provider.fallback_models == []

    def test_start_provider_default_control(self, ws_env):
        """No overlay: START's provider carries the global policy's
        fallbacks (the same-provider alternative at minimum)."""
        calls, events = _drive_task(
            ws_env, patch_factory=False, fail_first=False)
        assert events[-1].type == EventType.TASK_COMPLETED
        provider = calls[0]["provider"]
        assert provider.fallback_models != []
