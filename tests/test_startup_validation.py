"""Tests for startup consistency matrix across config.json, SecretStore, and model registry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.routing.startup import validate_startup_configuration
from aether_engine.secrets.storage import SecretStore


@pytest.fixture
def secret_store(tmp_path):
    db_path = tmp_path / "secrets.db"
    store = SecretStore(db_path=db_path)
    return store


@pytest.fixture
def fake_keyring():
    """Dict-backed keyring (the test_protector_delete.py pattern).

    SecretStore.save_provider needs an OS keyring; environments
    without one (this VM, some CI images) raise NoKeyringError before
    any test logic runs. Patch in an in-memory keyring instead.
    """
    storage = {}

    def set_password(service, username, password):
        storage[(service, username)] = password

    def get_password(service, username):
        return storage.get((service, username))

    def delete_password(service, username):
        storage.pop((service, username), None)

    with (
        patch("keyring.set_password", side_effect=set_password),
        patch("keyring.get_password", side_effect=get_password),
        patch("keyring.delete_password", side_effect=delete_password),
        patch("sys.platform", "linux"),
    ):
        yield storage


@pytest.fixture
def model_registry():
    models = [
        ModelEntry(provider="google_gemini", id="gemini-2.5-flash", label="Gemini Flash", priority=1),
        ModelEntry(provider="google_gemini", id="gemini-2.0-flash", label="Gemini 2.0 Flash", priority=2),
        ModelEntry(provider="openai", id="gpt-4o-mini", label="GPT-4o Mini", priority=1),
    ]
    return ModelRegistry(models)


class TestStartupValidation:
    def test_provider_with_no_secret_falls_back_to_keyed_provider(self, fake_keyring, secret_store, model_registry):
        # Configure secret only for openai
        secret_store.save_provider("openai", "sk-test-key")
        health_mgr = ProviderHealthManager()

        # User config points to unkeyed google_gemini
        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=secret_store,
            health_manager=health_mgr,
            registry=model_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "openai"
        assert result.active_model == "gpt-4o-mini"
        assert "no stored API key" in result.fallback_reason

    def test_missing_model_falls_back_to_provider_default(self, fake_keyring, secret_store, model_registry):
        secret_store.save_provider("google_gemini", "dummy-gemini-key")
        health_mgr = ProviderHealthManager()

        cfg = UserConfig(default_provider="google_gemini", default_model="non-existent-model-xyz")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=secret_store,
            health_manager=health_mgr,
            registry=model_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "google_gemini"
        assert result.active_model == "gemini-2.5-flash"
        assert "not found for provider" in result.fallback_reason

    def test_offline_default_provider_selects_next_healthy_provider(self, fake_keyring, secret_store, model_registry):
        secret_store.save_provider("google_gemini", "gemini-key")
        secret_store.save_provider("openai", "openai-key")

        health_mgr = ProviderHealthManager()
        health_mgr.record_failure("google_gemini", "Endpoint down", is_retriable=False)

        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=secret_store,
            health_manager=health_mgr,
            registry=model_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "openai"
        assert result.active_model == "gpt-4o-mini"
        assert "offline" in result.fallback_reason

    def test_perfect_consistency(self, fake_keyring, secret_store, model_registry):
        secret_store.save_provider("google_gemini", "gemini-key")
        health_mgr = ProviderHealthManager()

        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=secret_store,
            health_manager=health_mgr,
            registry=model_registry,
        )

        assert result.was_fallback is False
        assert result.active_provider == "google_gemini"
        assert result.active_model == "gemini-2.5-flash"
        assert result.fallback_reason is None


class TestDefaultModelSurvivesDiscoveryReplace:
    """B10, investigation Case C: the user picked a discovered-only
    model; a later discovery returns a list that omits it (delisted /
    renamed upstream). The replace must keep the configured default
    anchored in the persisted provider_models, so the next launch's
    startup validation initialises with the user's choice — not the
    registry default it silently substituted before the fix."""

    def test_discovery_replace_keeps_configured_default(self, fake_keyring, secret_store):
        from aether_engine.orchestration.core import (
            _discover_cloud_provider_models,
            clear_discovery_failure,
        )
        from aether_engine.providers.discovery import DiscoveryResult
        from aether_engine.state import engine_state

        secret_store.save_provider("google_gemini", "dummy-gemini-key")

        cfg = engine_state.user_config
        snap = {
            "default_provider": cfg.default_provider,
            "default_model": cfg.default_model,
            "provider_models": {k: list(v) for k, v in cfg.provider_models.items()},
            "provider_excluded_models": {
                k: list(v) for k, v in cfg.provider_excluded_models.items()
            },
        }
        try:
            # End of session N: the choice and the persisted list
            # containing it are on disk.
            cfg.default_provider = "google_gemini"
            cfg.default_model = "gemini-3.5-flash-lite"
            cfg.provider_models = {
                "google_gemini": [
                    "gemini-3.5-flash",
                    "gemini-2.5-pro",
                    "gemini-3.5-flash-lite",
                ],
            }
            save_config(cfg)

            # Session N+1 discovery: the provider no longer returns
            # the chosen model.
            clear_discovery_failure("google_gemini")
            disc = DiscoveryResult(
                models=["gemini-3.5-flash", "gemini-2.5-pro"], excluded=[])
            with patch(
                "aether_engine.orchestration.core.fetch_available_models",
                new=AsyncMock(return_value=disc),
            ):
                changed = asyncio.run(
                    _discover_cloud_provider_models(
                        secret_store, SimpleNamespace(name="google_gemini"))
                )
            assert changed is True
            assert "gemini-3.5-flash-lite" in cfg.provider_models["google_gemini"]

            # Reload + re-register exactly as EngineState.initialize /
            # resync_active_state does, then validate.
            reloaded = load_config()
            assert reloaded.default_model == "gemini-3.5-flash-lite"
            registry = ModelRegistry()
            registry.update_provider_models(
                "google_gemini", reloaded.provider_models.get("google_gemini", []))
            result = validate_startup_configuration(
                user_config=reloaded,
                secret_store=secret_store,
                health_manager=ProviderHealthManager(),
                registry=registry,
            )
            assert result.was_fallback is False
            assert result.active_provider == "google_gemini"
            assert result.active_model == "gemini-3.5-flash-lite"
        finally:
            cfg.default_provider = snap["default_provider"]
            cfg.default_model = snap["default_model"]
            cfg.provider_models = snap["provider_models"]
            cfg.provider_excluded_models = snap["provider_excluded_models"]
