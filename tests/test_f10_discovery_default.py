"""F-10 (EGM Blocker C-B14): discovery must not resurrect gemini-2.5-flash.

Settled decision (ADR 0033): the default is gemini-3.5-flash;
gemini-2.5-flash returns 404 on the owner's key and must never become
the effective default / priority-1 model by discovery alone.

Bugs covered:
- providers/discovery.py sorted gemini-2.5-flash to the front of the
  discovered Gemini list (priority 1), ahead of gemini-3.5-flash.
- routing/registry.py update_provider_models assigned priority purely
  from discovered-list order, replacing curated priority.
- app.py lifespan overwrote the persisted UserConfig.default_model
  with disc.models[0] when the persisted model was absent from the
  discovered list (or contained "lite").
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.providers.discovery import (
    DiscoveryResult,
    _discover_gemini_models,
    fetch_available_models,
)
from aether_engine.routing.registry import ModelRegistry


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _mock_get(fake_response, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = fake_response
    mock_resp.text = "mock body"
    return mock_resp


def _gemini_payload(*model_ids):
    return {
        "models": [
            {
                "name": f"models/{mid}",
                "displayName": mid,
                "supportedGenerationMethods": ["generateContent"],
            }
            for mid in model_ids
        ]
    }


# ---------------------------------------------------------------------------
# (a) Discovery ordering / registry priority
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discovery_does_not_put_gemini_25_flash_first():
    """Provider returns 2.5-flash first; discovery must not keep it first."""
    payload = _gemini_payload(
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-2.0-flash",
        "gemini-2.5-pro",
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        models, _excluded = await _discover_gemini_models("fake-key")

    assert "gemini-2.5-flash" in models
    assert "gemini-3.5-flash" in models
    assert models[0] == "gemini-3.5-flash", (
        f"curated default must lead discovery, got order: {models}"
    )
    assert models.index("gemini-3.5-flash") < models.index("gemini-2.5-flash")


@pytest.mark.asyncio
async def test_fetch_available_models_keeps_curated_default_first():
    payload = _gemini_payload("gemini-2.5-flash", "gemini-3.5-flash")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(payload)
        result = await fetch_available_models("google_gemini", "fake-key")

    assert result.ok
    assert result.models[0] == "gemini-3.5-flash"


def test_registry_update_does_not_promote_gemini_25_flash():
    """Even if a discovered list arrives with 2.5-flash first, the
    registry must preserve curated priority (3.5-flash is priority 1)."""
    registry = ModelRegistry()
    registry.update_provider_models(
        "google_gemini", ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.0-flash"]
    )

    default_entry = registry.get_default_model_for_provider("google_gemini")
    assert default_entry is not None
    assert default_entry.id == "gemini-3.5-flash"

    entry_35 = registry.get_model("gemini-3.5-flash")
    entry_25 = registry.get_model("gemini-2.5-flash")
    assert entry_35 is not None and entry_25 is not None
    assert entry_35.priority < entry_25.priority
    ordered = [m.id for m in registry.get_models_for_provider("google_gemini")]
    assert ordered[0] == "gemini-3.5-flash"


def test_registry_update_preserves_input_order_for_unknown_models():
    """Non-curated models keep their discovered relative order."""
    registry = ModelRegistry()
    registry.update_provider_models("google_gemini", ["gemini-test-1", "gemini-test-2"])
    ordered = [m.id for m in registry.get_models_for_provider("google_gemini")]
    assert ordered == ["gemini-test-1", "gemini-test-2"]


# ---------------------------------------------------------------------------
# (b) App startup: persisted default wins over discovery
# ---------------------------------------------------------------------------

class TestStartupDiscoveryDoesNotOverwritePersistedDefault:
    @pytest.fixture
    def _startup_env(self, tmp_path):
        from aether_engine.state import engine_state

        # Snapshot global state mutated by the lifespan.
        orig_cfg = engine_state.user_config
        orig_active_provider = engine_state.active_provider
        orig_active_model = engine_state.active_model
        orig_registry_models = list(engine_state.model_registry._models)
        orig_token_path = engine_state.token_path
        orig_audit_path = engine_state.audit_logger.log_path

        engine_state.token_path = tmp_path / "engine.token"
        engine_state.audit_logger.log_path = tmp_path / "audit.log"
        try:
            yield engine_state
        finally:
            engine_state.user_config = orig_cfg
            engine_state.active_provider = orig_active_provider
            engine_state.active_model = orig_active_model
            engine_state.model_registry._models = orig_registry_models
            engine_state.model_registry._by_id = {m.id: m for m in orig_registry_models}
            engine_state.token_path = orig_token_path
            engine_state.audit_logger.log_path = orig_audit_path

    def _run_startup(self, engine_state, *, persisted_model, discovered_models):
        from starlette.testclient import TestClient

        from aether_engine.app import app
        from aether_engine.config import UserConfig, load_config, save_config

        # Persist the user's chosen default exactly as config.json would.
        save_config(UserConfig(
            default_provider="google_gemini",
            default_model=persisted_model,
        ))

        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.load_provider.return_value = "fake-key"

        disc = DiscoveryResult(models=list(discovered_models), excluded=[])
        with patch("aether_engine.app.SecretStore", return_value=mock_store), \
             patch("aether_engine.app.fetch_available_models",
                   new=AsyncMock(return_value=disc)), TestClient(app):
            pass  # entering runs the full lifespan incl. discovery

        # What the lifespan left in memory and what it persisted to disk.
        return engine_state.user_config.default_model, engine_state.active_model, load_config().default_model

    def test_persisted_default_survives_discovery_with_different_first_model(
        self, _startup_env
    ):
        engine_state = _startup_env
        # disc.models[0] is the 404 model; the persisted default is not
        # even in the discovered list (the old overwrite trigger).
        cfg_default, _active, persisted_on_disk = self._run_startup(
            engine_state,
            persisted_model="gemini-3.5-flash",
            discovered_models=["gemini-2.5-flash", "gemini-2.0-flash"],
        )
        assert cfg_default == "gemini-3.5-flash"
        assert persisted_on_disk == "gemini-3.5-flash"

    def test_persisted_lite_default_survives_discovery(self, _startup_env):
        engine_state = _startup_env
        # Old code also overwrote any persisted model containing "lite",
        # even when it was present in the discovered list.
        cfg_default, _active, persisted_on_disk = self._run_startup(
            engine_state,
            persisted_model="gemini-2.0-flash-lite",
            discovered_models=["gemini-2.5-flash", "gemini-2.0-flash-lite"],
        )
        assert cfg_default == "gemini-2.0-flash-lite"
        assert persisted_on_disk == "gemini-2.0-flash-lite"


# ---------------------------------------------------------------------------
# (c) F-10 residual: the WebSocket provider handlers must resolve the
# default through the registry, never from raw list position [0].
#
# PROVIDER_SAVE_REQUEST (is_default, no explicit model) and
# PROVIDER_REMOVE_REQUEST (replacing a removed default provider) both
# assigned ``default_model`` from ``provider_models[...][0]`` — a raw,
# possibly stale/client-supplied list whose order is NOT registry
# priority. With a pre-fix persisted list (2.5-flash first) in a
# discovery-less session, either handler silently re-pinned the 404
# model as the default, bypassing the registry protection above.
# ---------------------------------------------------------------------------

class TestProviderHandlersResolveDefaultViaRegistry:
    @pytest.fixture
    def _ws_env(self, tmp_path):
        from aether_engine.state import engine_state

        orig_cfg = engine_state.user_config
        orig_active_provider = engine_state.active_provider
        orig_active_model = engine_state.active_model
        orig_configured = list(engine_state.configured_providers)
        orig_registry_models = list(engine_state.model_registry._models)
        orig_token_path = engine_state.token_path
        orig_audit_path = engine_state.audit_logger.log_path

        engine_state.token_path = tmp_path / "engine.token"
        engine_state.audit_logger.log_path = tmp_path / "audit.log"
        engine_state.initialize_auth()
        try:
            yield engine_state
        finally:
            engine_state.user_config = orig_cfg
            engine_state.active_provider = orig_active_provider
            engine_state.active_model = orig_active_model
            engine_state.configured_providers = orig_configured
            engine_state.model_registry._models = orig_registry_models
            engine_state.model_registry._by_id = {
                m.id: m for m in orig_registry_models
            }
            engine_state.token_path = orig_token_path
            engine_state.audit_logger.log_path = orig_audit_path

    def test_save_as_default_uses_registry_priority_not_list_order(self, _ws_env):
        from unittest.mock import MagicMock, patch

        from aether_common.contracts import Event, EventType

        engine_state = _ws_env
        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.load_provider.return_value = "fake-key"

        with patch("aether_engine.routes.websocket.SecretStore",
                   return_value=mock_store):
            from starlette.testclient import TestClient

            from aether_engine.app import app

            with TestClient(app) as client, client.websocket_connect(
                    "/ws/tasks",
                    headers={
                        "Authorization":
                            f"Bearer {engine_state.auth_token}",
                    }) as ws:
                ws.receive_text()  # HELLO
                # Stale, unsorted persisted list: the 404 model
                # first. The registry knows curated priority.
                cfg = engine_state.user_config
                cfg.provider_models["google_gemini"] = [
                    "gemini-2.5-flash", "gemini-3.5-flash",
                ]
                engine_state.model_registry.update_provider_models(
                    "google_gemini",
                    ["gemini-2.5-flash", "gemini-3.5-flash"],
                )
                ws.send_text(Event(
                    type=EventType.PROVIDER_SAVE_REQUEST,
                    payload={
                        "provider": "google_gemini",
                        "api_key": "",
                        "is_default": True,
                        "default_model": "",
                        "models": [],
                    },
                ).to_json())
                resp = Event.from_json(ws.receive_text())

        assert resp.type == EventType.PROVIDER_SAVE_RESPONSE
        assert resp.payload["success"] is True
        assert engine_state.user_config.default_provider == "google_gemini"
        assert engine_state.user_config.default_model == "gemini-3.5-flash"
        assert engine_state.active_model == "gemini-3.5-flash"

    def test_remove_default_provider_uses_registry_priority(self, _ws_env):
        from unittest.mock import MagicMock, patch

        from aether_common.contracts import Event, EventType

        engine_state = _ws_env
        mock_store = MagicMock()
        mock_store.list_providers.return_value = ["google_gemini"]
        mock_store.delete_provider.return_value = True
        mock_store.load_provider.return_value = "fake-key"

        with patch("aether_engine.routes.websocket.SecretStore",
                   return_value=mock_store):
            from starlette.testclient import TestClient

            from aether_engine.app import app

            with TestClient(app) as client, client.websocket_connect(
                    "/ws/tasks",
                    headers={
                        "Authorization":
                            f"Bearer {engine_state.auth_token}",
                    }) as ws:
                ws.receive_text()  # HELLO
                cfg = engine_state.user_config
                cfg.default_provider = "openai"
                cfg.default_model = "gpt-4o"
                cfg.provider_models["openai"] = ["gpt-4o"]
                # Replacement provider's persisted list is stale
                # and unsorted: the 404 model first.
                cfg.provider_models["google_gemini"] = [
                    "gemini-2.5-flash", "gemini-3.5-flash",
                ]
                engine_state.model_registry.update_provider_models(
                    "google_gemini",
                    ["gemini-2.5-flash", "gemini-3.5-flash"],
                )
                engine_state.configured_providers = ["openai", "google_gemini"]
                ws.send_text(Event(
                    type=EventType.PROVIDER_REMOVE_REQUEST,
                    payload={"provider": "openai"},
                ).to_json())
                resp = Event.from_json(ws.receive_text())

        assert resp.type == EventType.PROVIDER_REMOVE_RESPONSE
        assert resp.payload["success"] is True
        assert engine_state.user_config.default_provider == "google_gemini"
        assert engine_state.user_config.default_model == "gemini-3.5-flash"
        assert engine_state.active_model == "gemini-3.5-flash"
