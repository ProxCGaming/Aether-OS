"""Wave 1, Slice 1 (runtime/UX fix programme): startup provider
discovery must run in the background, not inline in the lifespan.

Before this fix the lifespan awaited fetch_available_models for every
configured provider (10s timeout each, sequential) before yielding, so
/health waited on the slowest provider set. These tests stub discovery
to be slow and prove:
  (a) app startup completes and /health answers while discovery is
      still pending;
  (b) once discovery finishes, its config effect (provider model list
      + save_config) lands exactly as before;
  (c) a discovery failure is logged and never takes the engine down.
"""
import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

DISCOVERY_DELAY_S = 4.0
STARTUP_BUDGET_S = 3.0


@pytest.fixture
def discovery_harness():
    """Snapshot/restore the config fields startup discovery mutates."""
    from aether_engine.app import engine_state

    orig_models = dict(engine_state.user_config.provider_models)
    orig_excluded = {
        k: list(v) for k, v in engine_state.user_config.provider_excluded_models.items()
    }
    mock_store = MagicMock()
    mock_store.list_providers.return_value = ["openai"]
    mock_store.load_provider.return_value = "sk-test"
    try:
        yield engine_state, mock_store
    finally:
        engine_state.user_config.provider_models = orig_models
        engine_state.user_config.provider_excluded_models = orig_excluded


def test_health_answers_while_startup_discovery_still_pending(discovery_harness):
    from aether_engine.app import app
    from aether_engine.providers.discovery import DiscoveryResult

    engine_state, mock_store = discovery_harness

    async def slow_fetch(provider, key, base_url=None, provider_flavor=None):
        import asyncio

        await asyncio.sleep(DISCOVERY_DELAY_S)
        return DiscoveryResult(models=["stub-model-1"], excluded=[])

    with (
        patch("aether_engine.app.SecretStore", return_value=mock_store),
        patch("aether_engine.app.fetch_available_models", new=slow_fetch),
        patch("aether_engine.app.save_config") as mock_save,
    ):
        started = time.monotonic()
        with TestClient(app) as client:
            startup_s = time.monotonic() - started
            # (a) Startup finished well inside one discovery delay even
            # though two providers (openai + custom_openai) are
            # discovered sequentially, and /health already answers.
            assert startup_s < STARTUP_BUDGET_S
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            # Discovery is still pending: its effect has not landed yet.
            assert engine_state.user_config.provider_models.get("openai") != [
                "stub-model-1"
            ]
            saves_at_startup = mock_save.call_count

            # (b) Once discovery is allowed to finish, its config
            # effect is applied exactly as the old inline loop did it.
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if engine_state.user_config.provider_models.get("openai") == [
                    "stub-model-1"
                ]:
                    break
                time.sleep(0.1)
            assert engine_state.user_config.provider_models.get("openai") == [
                "stub-model-1"
            ]
            assert mock_save.call_count > saves_at_startup


def test_startup_discovery_failure_is_logged_and_engine_stays_up(
    discovery_harness, caplog
):
    from aether_engine.app import app

    _engine_state, mock_store = discovery_harness

    async def failing_fetch(provider, key, base_url=None, provider_flavor=None):
        raise RuntimeError("provider exploded")

    with (
        patch("aether_engine.app.SecretStore", return_value=mock_store),
        patch("aether_engine.app.fetch_available_models", new=failing_fetch),
        patch("aether_engine.app.save_config"),
        caplog.at_level(logging.WARNING, logger="aether_engine.app"),
        TestClient(app) as client,
    ):
        assert client.get("/health").status_code == 200
        # Give the background task time to run and fail.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if any(
                "startup model discovery" in r.getMessage().lower()
                for r in caplog.records
            ):
                break
            time.sleep(0.1)
        assert client.get("/health").status_code == 200
    assert any(
        "startup model discovery" in r.getMessage().lower() for r in caplog.records
    )
