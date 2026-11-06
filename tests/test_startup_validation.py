"""Tests for startup consistency matrix across config.json, SecretStore, and model registry."""
from unittest.mock import patch

import pytest

from aether_engine.config import UserConfig
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
