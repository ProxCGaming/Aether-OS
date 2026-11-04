"""Wave 1, Slice 2a — RUX-B-01 / RUX-B-02: provider discovery off the WS loop.

- RUX-B-01: REFRESH_MODELS_REQUEST runs in a background task. An
  INSTANT message (MODEL_SET_DEFAULT) sent right behind a refresh
  whose discovery hangs must be answered BEFORE the refresh response.
- RUX-B-02: PROVIDER_LIST_REQUEST answers immediately from the model
  cache; discovery for uncached providers runs in a background task
  and pushes a refreshed PROVIDER_LIST_RESPONSE when it lands.
- Negative caching: a failed discovery is recorded and not retried
  within DISCOVERY_FAILURE_TTL_SECONDS; a successful discovery
  clears the record.

Harness follows the E5-F07/epic5-minors ws_env pattern: tmp
CONFIG_PATH, scripted registry/health manager, mocked SecretStore in
the websocket module's namespace. No live network: every discovery
fetch is a scripted fake.
"""
import asyncio
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.orchestration.core as core_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, save_config
from aether_engine.memory import session_store
from aether_engine.providers.discovery import DiscoveryError, DiscoveryResult
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

PROVIDERS = ["google_gemini", "openai", "anthropic"]


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-main", label="Gemini Main",
                   capabilities=["chat", "code", "tools"], priority=1),
        ModelEntry(provider="openai", id="gpt-main", label="GPT Main",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="anthropic", id="claude-main", label="Claude Main",
                   capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    cfg = UserConfig(
        default_provider="google_gemini", default_model="gemini-main",
        workspace_roots=[str(tmp_path)])
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
    monkeypatch.setattr(engine_state, "user_config", cfg)
    # The negative cache is module-global: never leak records between tests.
    failures = getattr(core_module, "_discovery_failures", None)
    if failures is not None:
        failures.clear()
    yield {"tmp_path": tmp_path, "config": cfg, "config_path": config_path}
    if failures is not None:
        failures.clear()


def _connect():
    return TestClient(app)


def _mock_store(stack, providers):
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = providers
    mock_store.load_provider.return_value = "fake-key"
    return mock_store


def _connect_ws(stack):
    client = stack.enter_context(_connect())
    return stack.enter_context(client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ))


def _seed_cached_models(ws_env, models):
    cfg = ws_env["config"]
    cfg.provider_models.update(models)
    save_config(cfg, ws_env["config_path"])


def _provider_entry(providers, name):
    return next(p for p in providers if p["name"] == name)


# ---------------------------------------------------------------------------
# RUX-B-01 — REFRESH_MODELS must not block the message loop
# ---------------------------------------------------------------------------


class TestRefreshModelsOffLoop:
    def test_instant_message_answered_before_refresh_response(self, ws_env):
        # google_gemini + anthropic cached; openai is the refresh target
        # whose discovery hangs for 3 s.
        _seed_cached_models(ws_env, {
            "google_gemini": ["gemini-main"],
            "anthropic": ["claude-main"],
        })

        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(3)
            return DiscoveryResult(models=["gpt-slow"], excluded=[], error=None)

        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            ws_module, "fetch_available_models", AsyncMock(side_effect=slow_fetch)))
        stack.enter_context(patch.object(
            core_module, "fetch_available_models",
            AsyncMock(return_value=DiscoveryResult(models=["unused"], excluded=[], error=None))))
        with stack:
            ws = _connect_ws(stack)
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.REFRESH_MODELS_REQUEST,
                payload={"provider": "openai"},
            ).to_json())
            ws.send_text(Event(
                type=EventType.MODEL_SET_DEFAULT,
                payload={"provider": "google_gemini", "model": "gemini-2.5-pro"},
            ).to_json())

            seen = []
            refresh_ev = None
            for _ in range(10):
                ev = Event.from_json(ws.receive_text())
                seen.append(ev.type)
                if ev.type == EventType.REFRESH_MODELS_RESPONSE:
                    refresh_ev = ev
                if refresh_ev is not None and EventType.MODEL_DEFAULT_CHANGED in seen:
                    break

            assert EventType.MODEL_DEFAULT_CHANGED in seen
            assert refresh_ev is not None
            # The whole point of RUX-B-01: the instant message is NOT
            # stuck behind the refresh's discovery.
            assert seen.index(EventType.MODEL_DEFAULT_CHANGED) < seen.index(
                EventType.REFRESH_MODELS_RESPONSE)
            openai_entry = _provider_entry(refresh_ev.payload["providers"], "openai")
            assert openai_entry["models"] == ["gpt-slow"]


# ---------------------------------------------------------------------------
# RUX-B-02 — PROVIDER_LIST answers from cache, discovery in background
# ---------------------------------------------------------------------------


class TestProviderListOffLoop:
    def test_list_answers_from_cache_then_pushes_discovered_update(self, ws_env):
        _seed_cached_models(ws_env, {
            "google_gemini": ["gemini-main"],
            "anthropic": ["claude-main"],
        })

        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(4)
            return DiscoveryResult(models=["gpt-slow"], excluded=[], error=None)

        fetch_mock = AsyncMock(side_effect=slow_fetch)
        stack = ExitStack()
        _mock_store(stack, PROVIDERS)
        stack.enter_context(patch.object(
            core_module, "fetch_available_models", fetch_mock))
        with stack:
            ws = _connect_ws(stack)
            ws.receive_text()  # HELLO

            start = time.monotonic()
            ws.send_text(Event(type=EventType.PROVIDER_LIST_REQUEST).to_json())
            first = Event.from_json(ws.receive_text())
            elapsed = time.monotonic() - start

            assert first.type == EventType.PROVIDER_LIST_RESPONSE
            # Prompt: well under the 4 s discovery hang (and the 10 s
            # DISCOVERY_TIMEOUT the old inline path could cost).
            assert elapsed < 2.0
            openai_first = _provider_entry(first.payload["providers"], "openai")
            assert openai_first["models"] == []

            # The background discovery lands and pushes a refreshed list.
            second = Event.from_json(ws.receive_text())
            assert second.type == EventType.PROVIDER_LIST_RESPONSE
            openai_second = _provider_entry(second.payload["providers"], "openai")
            assert openai_second["models"] == ["gpt-slow"]
            assert fetch_mock.await_count == 1


# ---------------------------------------------------------------------------
# Negative caching (core-level)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, providers):
        self._providers = providers

    def list_providers(self):
        return list(self._providers)

    def load_provider(self, name):
        return "fake-key" if name in self._providers else ""


class TestDiscoveryNegativeCache:
    async def _build(self, store):
        return await core_module._build_provider_list_async(store)

    @pytest.mark.asyncio
    async def test_failed_provider_fetched_once_within_ttl(self, ws_env):
        fetch_mock = AsyncMock(return_value=DiscoveryResult(
            models=[], excluded=[],
            error=DiscoveryError(code=503, message="provider down")))
        with patch.object(core_module, "fetch_available_models", fetch_mock):
            store = _FakeStore(["openai"])
            first = await self._build(store)
            second = await self._build(store)

        assert fetch_mock.await_count == 1
        assert "openai" in core_module._discovery_failures
        for listing in (first, second):
            openai_entry = _provider_entry(listing, "openai")
            assert openai_entry["models"] == []

    @pytest.mark.asyncio
    async def test_raising_fetch_also_negative_cached(self, ws_env):
        fetch_mock = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(core_module, "fetch_available_models", fetch_mock):
            store = _FakeStore(["openai"])
            await self._build(store)
            await self._build(store)

        assert fetch_mock.await_count == 1
        assert "openai" in core_module._discovery_failures

    @pytest.mark.asyncio
    async def test_success_after_ttl_clears_failure_record(self, ws_env):
        # A failure older than the TTL no longer suppresses discovery…
        core_module._discovery_failures["openai"] = (
            time.monotonic() - core_module.DISCOVERY_FAILURE_TTL_SECONDS - 1.0)
        fetch_mock = AsyncMock(return_value=DiscoveryResult(
            models=["m1"], excluded=[], error=None))
        with patch.object(core_module, "fetch_available_models", fetch_mock):
            store = _FakeStore(["openai"])
            listing = await self._build(store)
            # …and once discovered, the models are cached, so a second
            # list does not fetch again either.
            await self._build(store)

        assert fetch_mock.await_count == 1
        assert "openai" not in core_module._discovery_failures
        openai_entry = _provider_entry(listing, "openai")
        assert openai_entry["models"] == ["m1"]
