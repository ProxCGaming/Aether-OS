"""Tests for startup consistency matrix across config.json, SecretStore, and model registry."""
import pytest

from aether_engine.config import UserConfig
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.routing.startup import validate_startup_configuration
from aether_engine.secrets.storage import SecretStore


@pytest.fixture
def test_secrets(tmp_path):
    db_path = tmp_path / "secrets.db"
    store = SecretStore(db_path=db_path)
    return store


@pytest.fixture
def test_registry():
    models = [
        ModelEntry(provider="google_gemini", id="gemini-2.5-flash", label="Gemini Flash", priority=1),
        ModelEntry(provider="google_gemini", id="gemini-2.0-flash", label="Gemini 2.0 Flash", priority=2),
        ModelEntry(provider="openai", id="gpt-4o-mini", label="GPT-4o Mini", priority=1),
    ]
    return ModelRegistry(models)


class TestStartupValidation:
    def test_provider_with_no_secret_falls_back_to_keyed_provider(self, test_secrets, test_registry):
        # Configure secret only for openai
        test_secrets.save_provider("openai", "sk-test-key")
        health_mgr = ProviderHealthManager()

        # User config points to unkeyed google_gemini
        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=test_secrets,
            health_manager=health_mgr,
            registry=test_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "openai"
        assert result.active_model == "gpt-4o-mini"
        assert "no stored API key" in result.fallback_reason

    def test_missing_model_falls_back_to_provider_default(self, test_secrets, test_registry):
        test_secrets.save_provider("google_gemini", "dummy-gemini-key")
        health_mgr = ProviderHealthManager()

        cfg = UserConfig(default_provider="google_gemini", default_model="non-existent-model-xyz")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=test_secrets,
            health_manager=health_mgr,
            registry=test_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "google_gemini"
        assert result.active_model == "gemini-2.5-flash"
        assert "not found for provider" in result.fallback_reason

    def test_offline_default_provider_selects_next_healthy_provider(self, test_secrets, test_registry):
        test_secrets.save_provider("google_gemini", "gemini-key")
        test_secrets.save_provider("openai", "openai-key")

        health_mgr = ProviderHealthManager()
        health_mgr.record_failure("google_gemini", "Endpoint down", is_retriable=False)

        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=test_secrets,
            health_manager=health_mgr,
            registry=test_registry,
        )

        assert result.was_fallback is True
        assert result.active_provider == "openai"
        assert result.active_model == "gpt-4o-mini"
        assert "offline" in result.fallback_reason

    def test_perfect_consistency(self, test_secrets, test_registry):
        test_secrets.save_provider("google_gemini", "gemini-key")
        health_mgr = ProviderHealthManager()

        cfg = UserConfig(default_provider="google_gemini", default_model="gemini-2.5-flash")

        result = validate_startup_configuration(
            user_config=cfg,
            secret_store=test_secrets,
            health_manager=health_mgr,
            registry=test_registry,
        )

        assert result.was_fallback is False
        assert result.active_provider == "google_gemini"
        assert result.active_model == "gemini-2.5-flash"
        assert result.fallback_reason is None
