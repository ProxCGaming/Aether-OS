"""Tests for the discovery v2 behavior (user decision, Oct 2026):

- fetch_available_models returns a DiscoveryResult with BOTH the filtered
  chat-capable list and the excluded list (kind-tagged).
- No hardcoded fallback model lists: failures surface a DiscoveryError
  carrying the HTTP status code.
- _validate_provider_key pings only the filtered list; failures surface the
  discovery error with its code.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aether_engine.providers.discovery import (
    DiscoveryError,
    DiscoveryResult,
    fetch_available_models,
)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """This VM has malformed proxy env vars that break httpx client
    construction; strip them so the mocked HTTP layer is exercised."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _mock_resp(status_code=200, payload=None, text="mock body"):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = payload if payload is not None else {}
    mock_resp.text = text
    return mock_resp


@pytest.mark.asyncio
async def test_discovery_result_shape():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "text-embedding-3-small"}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_resp(200, payload)
        result = await fetch_available_models("openai", "sk-x")
    assert isinstance(result, DiscoveryResult)
    assert result.ok
    assert result.error is None
    assert result.models == ["gpt-4o"]
    assert result.excluded == [{"id": "text-embedding-3-small", "kind": "embedding"}]


@pytest.mark.asyncio
async def test_no_fallback_on_http_error():
    """A 403 must come back as an error with its code — never a fake list."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_resp(403, {"error": "forbidden"}, "forbidden")
        result = await fetch_available_models("openai", "sk-x")
    assert result.models == []
    assert result.excluded == []
    assert not result.ok
    assert result.error.code == 403
    assert "403" in result.error.message


@pytest.mark.asyncio
async def test_validate_surfaces_discovery_error_with_code():
    """Discovery fails (401) and the default-model ping also fails: the
    result must carry the discovery error with its HTTP code."""
    from aether_engine.orchestration.core import _validate_provider_key
    from aether_engine.state import engine_state

    async def fake_validate(provider, key, model="", base_url=None):
        return {"status": "network_error", "message": "boom"}

    with patch(
        "aether_engine.orchestration.core.fetch_available_models",
        new=AsyncMock(return_value=DiscoveryResult(
            error=DiscoveryError(code=401, message="bad key"))),
    ), patch(
        "aether_engine.orchestration.core.validate_api_key",
        new=AsyncMock(side_effect=fake_validate),
    ), patch(
        "aether_engine.orchestration.core.save_config"
    ), patch.object(
        engine_state.health_manager, "record_success"
    ), patch.object(
        engine_state.health_manager, "record_failure"
    ), patch.object(
        engine_state.model_registry, "update_provider_models"
    ):
        result = await _validate_provider_key("openai", "sk-bad")
    assert result["status"] == "network_error"
    assert result["discovery_error"]["code"] == 401
    assert "401" in result["message"]
    assert "success" not in result  # success flag is added by the WS layer


@pytest.mark.asyncio
async def test_validate_connected_when_ping_works_despite_discovery_error():
    """Key is valid even if /models is unreachable: the default-model ping
    still connects."""
    from aether_engine.orchestration.core import _validate_provider_key
    from aether_engine.state import engine_state

    async def fake_validate(provider, key, model="", base_url=None):
        return {"status": "connected", "message": "ok", "latency_ms": 5.0}

    with patch(
        "aether_engine.orchestration.core.fetch_available_models",
        new=AsyncMock(return_value=DiscoveryResult(
            error=DiscoveryError(code=None, message="dns fail"))),
    ), patch(
        "aether_engine.orchestration.core.validate_api_key",
        new=AsyncMock(side_effect=fake_validate),
    ), patch(
        "aether_engine.orchestration.core.save_config"
    ), patch.object(
        engine_state.health_manager, "record_success"
    ), patch.object(
        engine_state.health_manager, "record_failure"
    ), patch.object(
        engine_state.model_registry, "update_provider_models"
    ):
        result = await _validate_provider_key("openai", "sk-good")
    assert result["status"] == "connected"
    # The discovery error is still attached for transparency.
    assert result["discovery_error"]["code"] is None


@pytest.mark.asyncio
async def test_validate_attaches_excluded_models():
    from aether_engine.orchestration.core import _validate_provider_key
    from aether_engine.state import engine_state

    async def fake_validate(provider, key, model="", base_url=None):
        return {"status": "connected", "message": "ok", "latency_ms": 5.0}

    excluded = [{"id": "text-embedding-3-small", "kind": "embedding"}]
    with patch(
        "aether_engine.orchestration.core.fetch_available_models",
        new=AsyncMock(return_value=DiscoveryResult(models=["gpt-4o"], excluded=excluded)),
    ), patch(
        "aether_engine.orchestration.core.validate_api_key",
        new=AsyncMock(side_effect=fake_validate),
    ), patch(
        "aether_engine.orchestration.core.save_config"
    ), patch.object(
        engine_state.health_manager, "record_success"
    ), patch.object(
        engine_state.health_manager, "record_failure"
    ), patch.object(
        engine_state.model_registry, "update_provider_models"
    ):
        result = await _validate_provider_key(
            "openai", "sk-x", persist_models=False)
    assert result["models"] == ["gpt-4o"]
    assert result["excluded_models"] == excluded
    assert "discovery_error" not in result


def test_config_round_trips_excluded_models():
    from aether_engine.config import UserConfig

    cfg = UserConfig()
    cfg.provider_excluded_models["openai"] = [{"id": "text-embedding-3-small", "kind": "embedding"}]
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.provider_excluded_models["openai"] == [
        {"id": "text-embedding-3-small", "kind": "embedding"}
    ]


class TestDiscoveryWebSocket:
    """Save/list round-trips carry excluded models and real errors."""

    @pytest.fixture
    def ws_client(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from starlette.testclient import TestClient
        from aether_engine.app import app, engine_state

        token_file = tmp_path / "engine.token"
        audit_file = tmp_path / "audit.log"
        engine_state.token_path = token_file
        engine_state.audit_logger.log_path = audit_file
        engine_state.initialize_auth()

        orig_models = dict(engine_state.user_config.provider_models)
        orig_excluded = {k: list(v) for k, v in engine_state.user_config.provider_excluded_models.items()}
        # The WS handler instantiates SecretStore at connection time; mock it
        # before connecting (real keyring isn't available on this Linux VM).
        mock_store = MagicMock()
        mock_store.list_providers.return_value = []
        try:
            with patch("aether_engine.routes.websocket.SecretStore", return_value=mock_store), \
                 patch("aether_engine.app.SecretStore", return_value=mock_store):
                with TestClient(app) as client:
                    live_token = engine_state.auth_token
                    with client.websocket_connect(f"/ws/tasks?token={live_token}") as ws:
                        from aether_common.contracts import Event, EventType
                        hello = Event.from_json(ws.receive_text())
                        assert hello.type == EventType.HELLO
                        yield ws, engine_state
        finally:
            engine_state.user_config.provider_models = orig_models
            engine_state.user_config.provider_excluded_models = orig_excluded

    def _roundtrip(self, ws, event_type, payload):
        from aether_common.contracts import Event
        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_save_surfaces_discovery_error_with_code(self, ws_client):
        from aether_common.contracts import EventType

        ws, engine_state = ws_client
        disc = DiscoveryResult(error=DiscoveryError(code=403, message="forbidden"))
        with patch("aether_engine.routes.websocket.fetch_available_models",
                   new=AsyncMock(return_value=disc)):
            resp = self._roundtrip(ws, EventType.PROVIDER_SAVE_REQUEST,
                                   {"provider": "openai", "api_key": "sk-x"})
        assert resp.type == EventType.PROVIDER_SAVE_RESPONSE
        assert resp.payload["success"] is True  # key saved; list failed
        assert resp.payload["discovery_error"]["code"] == 403
        assert resp.payload["discovery_error"]["message"] == "forbidden"

    def test_provider_list_includes_excluded_models(self, ws_client):
        from aether_common.contracts import EventType

        ws, engine_state = ws_client
        engine_state.user_config.provider_models["openai"] = ["gpt-4o"]
        engine_state.user_config.provider_excluded_models["openai"] = [
            {"id": "text-embedding-3-small", "kind": "embedding"}
        ]
        resp = self._roundtrip(ws, EventType.PROVIDER_LIST_REQUEST, {})
        assert resp.type == EventType.PROVIDER_LIST_RESPONSE
        providers = {p["name"]: p for p in resp.payload["providers"]}
        assert providers["openai"]["excluded_models"] == [
            {"id": "text-embedding-3-small", "kind": "embedding"}
        ]
        # Chat selectors still only get the filtered list.
        assert providers["openai"]["models"] == ["gpt-4o"]
