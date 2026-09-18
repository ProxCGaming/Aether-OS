"""Regression tests for Providers panel fixes (blank tab, disconnect, status badge)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from aether_engine.app import _validate_provider_key, engine_state
from aether_engine.providers.discovery import (
    _discover_openai_compatible_models,
    fetch_available_models,
)
from aether_ui.settings_window import (
    AetherConfigWindow,
    ConnectedProviderRow,
    ProviderConfigDialog,
    QuickAddProviderDialog,
    _PROVIDERS_CONFIG,
    _stable_custom_key,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.mark.asyncio
async def test_openai_compatible_discovery_initializes_candidates():
    fake_response = {"data": [{"id": "llama-3.3-70b"}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        # Must not raise UnboundLocalError for `candidates`.
        models = await _discover_openai_compatible_models(
            base_url="https://example.com/v1", api_key="fake-key"
        )
        assert models == ["llama-3.3-70b"]


def test_user_config_tolerates_null_maps():
    from aether_engine.config import UserConfig
    cfg = UserConfig.from_dict({
        "provider_models": None,
        "custom_base_urls": None,
        "custom_provider_names": None,
        "custom_provider_types": None,
        "default_provider": None,
        "default_model": None,
    })
    assert cfg.provider_models == {}
    assert cfg.custom_base_urls == {}
    assert cfg.custom_provider_names == {}
    assert cfg.custom_provider_types == {}
    assert cfg.default_provider == "google_gemini"
    # Operations that previously crashed on None maps must now be safe.
    cfg.provider_models.get("x", [])
    cfg.custom_base_urls.pop("x", None)


def test_standard_provider_save_emits_cloud_type(qapp):
    dialog = ProviderConfigDialog(
        {"name": "openai", "display_name": "OpenAI", "provider_type": "cloud", "has_key": False}
    )
    captured = []
    dialog.save_requested.connect(lambda *args: captured.append(args))
    dialog.inp_key.setText("sk-openai")
    dialog._on_save(is_default=False)
    assert len(captured) == 1
    assert captured[0][0] == "openai"
    assert captured[0][6] == "cloud"
    assert captured[0][4] == ""


@pytest.mark.asyncio
async def test_custom_discovery_uses_base_url_for_non_prefixed_name():
    fake_response = {"data": [{"id": "llama-3.3-70b"}]}
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        models = await fetch_available_models(
            "my_lm_studio", "fake-key", base_url="https://example.com/v1"
        )
        assert models == ["llama-3.3-70b"]


def test_stable_custom_key_is_deterministic():
    assert _stable_custom_key("My Test Router") == _stable_custom_key("My Test Router")
    assert _stable_custom_key("My Test Router").startswith("custom_")
    assert _stable_custom_key("My Test Router") != _stable_custom_key("Other Router")


def test_quick_add_uses_stable_custom_key(qapp):
    dialog = QuickAddProviderDialog("openai_compatible")
    captured = []
    dialog.save_requested.connect(lambda *args: captured.append(args))
    dialog.inp_name.setText("My LM Studio")
    dialog.inp_url.setText("http://localhost:11434/v1")
    dialog._on_save()
    assert len(captured) == 1
    assert captured[0][0] == _stable_custom_key("My LM Studio")
    assert captured[0][5] == "My LM Studio"
    assert captured[0][6] == "openai_compatible"


def test_providers_tab_has_add_provider_button(qapp):
    win = AetherConfigWindow()
    texts = {b.text() for b in win.findChildren(QPushButton)}
    assert "+ Add Provider" in texts


@pytest.mark.asyncio
async def test_validate_continues_after_model_specific_invalid_key():
    calls = []

    async def fake_validate(provider, key, model="", base_url=None):
        calls.append(model)
        if model == "working-model":
            return {"status": "connected", "message": "ok", "latency_ms": 9.0}
        return {"status": "invalid_key", "message": "Invalid API key."}

    with patch("aether_engine.app.fetch_available_models",
               new=AsyncMock(return_value=["401-model", "working-model"])), \
         patch("aether_engine.app.validate_api_key", new=AsyncMock(side_effect=fake_validate)), \
         patch("aether_engine.app.save_config"), \
         patch.object(engine_state.health_manager, "record_success"), \
         patch.object(engine_state.health_manager, "record_failure"), \
         patch.object(engine_state.model_registry, "update_provider_models"):
        result = await _validate_provider_key(
            "custom_router", "sk-x", base_url="https://example.com/v1"
        )

    assert result["status"] == "connected"
    assert "working-model" in result["message"]
    assert calls == ["401-model", "working-model"]


@pytest.mark.asyncio
async def test_validate_custom_key_connected_when_discovery_succeeds():
    async def fake_validate(provider, key, model="", base_url=None):
        return {"status": "invalid_key", "message": "Invalid API key."}

    name = "custom_discovery_only"
    try:
        with patch("aether_engine.app.fetch_available_models",
                   new=AsyncMock(return_value=["m1", "m2", "m3"])), \
             patch("aether_engine.app.validate_api_key", new=AsyncMock(side_effect=fake_validate)), \
             patch("aether_engine.app.save_config"), \
             patch.object(engine_state.health_manager, "record_success"), \
             patch.object(engine_state.health_manager, "record_failure"), \
             patch.object(engine_state.model_registry, "update_provider_models"):
            result = await _validate_provider_key(
                name, "sk-x", base_url="https://example.com/v1", persist_models=False
            )
        assert result["status"] == "connected"
        assert "3 models available" in result["message"]
    finally:
        engine_state.user_config.provider_models.pop(name, None)


@pytest.mark.asyncio
async def test_validate_provider_key_retries_past_quota_models():
    discovered = ["quota-model-1", "quota-model-2", "working-model"]
    calls = []

    async def fake_validate(provider, key, model="", base_url=None):
        calls.append(model)
        if model == "working-model":
            return {"status": "connected", "message": "API key is valid.", "latency_ms": 12.0}
        return {"status": "network_error", "message": "quota exhausted"}

    with patch("aether_engine.app.fetch_available_models", new=AsyncMock(return_value=discovered)), \
         patch("aether_engine.app.validate_api_key", new=AsyncMock(side_effect=fake_validate)), \
         patch("aether_engine.app.save_config"), \
         patch.object(engine_state.health_manager, "record_success"), \
         patch.object(engine_state.health_manager, "record_failure"), \
         patch.object(engine_state.model_registry, "update_provider_models"):
        result = await _validate_provider_key(
            "custom_test", "sk-x", base_url="https://example.com/v1"
        )

    assert result["status"] == "connected"
    assert "working-model" in result["message"]
    assert calls == ["quota-model-1", "quota-model-2", "working-model"]
    assert result["models"] == discovered


@pytest.mark.asyncio
async def test_validate_provider_key_skips_persist_when_unregistered():
    async def fake_validate(provider, key, model="", base_url=None):
        return {"status": "connected", "message": "ok", "latency_ms": 5.0}

    with patch("aether_engine.app.fetch_available_models", new=AsyncMock(return_value=["m1", "m2"])), \
         patch("aether_engine.app.validate_api_key", new=AsyncMock(side_effect=fake_validate)), \
         patch("aether_engine.app.save_config"), \
         patch.object(engine_state.health_manager, "record_success"), \
         patch.object(engine_state.model_registry, "update_provider_models") as mock_update:
        result = await _validate_provider_key(
            "throwaway_template", "sk-x", base_url="https://example.com/v1", persist_models=False
        )

    assert result["status"] == "connected"
    assert "throwaway_template" not in engine_state.user_config.provider_models
    mock_update.assert_not_called()


def test_window_save_signal_forwards_display_name_and_type(qapp):
    win = AetherConfigWindow()
    captured = []
    win.save_requested.connect(lambda *args: captured.append(args))

    dialog = ProviderConfigDialog(
        {"name": "custom_openai", "display_name": "Custom OpenAI Compatible",
         "provider_type": "openai_compatible", "has_key": False, "base_url": ""},
        win,
    )
    dialog.save_requested.connect(win.save_requested)
    dialog.inp_name.setText("My Test Router")
    dialog.inp_base_url.setText("https://ps.example.com/v1")
    dialog.inp_key.setText("sk-abc")
    dialog._on_save(is_default=False)

    assert len(captured) == 1
    args = captured[0]
    assert len(args) == 7
    assert args[0].startswith("custom_") and args[0] != "custom_openai"
    assert args[4] == "https://ps.example.com/v1"
    assert args[5] == "My Test Router"
    assert args[6] == "openai_compatible"


def test_custom_save_key_is_stable_across_dialogs(qapp):
    def save_key():
        dialog = ProviderConfigDialog(
            {"name": "custom_openai", "display_name": "Custom OpenAI Compatible",
             "provider_type": "openai_compatible", "has_key": False, "base_url": ""},
        )
        captured = []
        dialog.save_requested.connect(lambda *args: captured.append(args))
        dialog.inp_name.setText("My Test Router")
        dialog.inp_base_url.setText("https://example.com/v1")
        dialog.inp_key.setText("sk-abc")
        dialog._on_save(is_default=False)
        assert len(captured) == 1
        return captured[0][0], dialog

    key1, dialog1 = save_key()
    key2, _ = save_key()
    assert key1 == key2
    assert key1.startswith("custom_") and key1 != "custom_openai"


def test_custom_save_requires_display_name(qapp):
    dialog = ProviderConfigDialog(
        {"name": "custom_openai", "display_name": "Custom OpenAI Compatible",
         "provider_type": "openai_compatible", "has_key": False, "base_url": ""},
    )
    emitted = []
    dialog.save_requested.connect(lambda *args: emitted.append(args))
    dialog.inp_base_url.setText("https://example.com/v1")
    dialog.inp_key.setText("sk-abc")
    dialog.inp_name.setText("")
    dialog._on_save(is_default=False)
    assert emitted == []
    assert "required" in dialog.lbl_status.text().lower() or "Error" in dialog.lbl_status.text()


def test_save_error_reenables_retry(qapp):
    dialog = ProviderConfigDialog(
        {"name": "custom_openai", "display_name": "Custom OpenAI Compatible",
         "provider_type": "openai_compatible", "has_key": False, "base_url": ""},
    )
    dialog.inp_name.setText("Router")
    dialog.inp_key.setText("sk-abc")
    dialog._on_save(is_default=False)
    assert dialog.btn_save.isEnabled() is False
    dialog.on_saved_error("boom")
    assert dialog.btn_save.isEnabled() is True
    assert "boom" in dialog.lbl_status.text()


def test_providers_tab_populated_on_init(qapp):
    win = AetherConfigWindow()
    # Label(s) + popular frame + stretch spacer.
    assert win.providers_layout.count() > 0
    assert win.providers_scroll_widget is not None


def test_populate_providers_empty_falls_back_to_defaults(qapp):
    win = AetherConfigWindow()
    win.populate_providers([])
    assert win.providers_layout.count() > 0
    for key, _name in _PROVIDERS_CONFIG:
        assert key in win.current_providers_data


def test_populate_providers_connected_and_unconnected(qapp):
    win = AetherConfigWindow()
    providers = [
        {"name": "google_gemini", "display_name": "Google Gemini", "has_key": True,
         "is_default": True, "models": ["gemini-2.0-flash"]},
        {"name": "openai", "display_name": "OpenAI", "has_key": False,
         "is_default": False, "models": []},
    ]
    win.populate_providers(providers)

    rows = win.providers_scroll_widget.findChildren(ConnectedProviderRow)
    assert len(rows) == 1
    assert rows[0].provider_key == "google_gemini"


def test_connected_row_disconnect_emits_key(qapp):
    row = ConnectedProviderRow({"name": "openai", "display_name": "OpenAI", "has_key": True})
    captured = []
    row.disconnect_clicked.connect(lambda k: captured.append(k))
    row.btn_disc.click()
    assert captured == ["openai"]


def test_connected_row_body_click_still_opens_dialog(qapp):
    row = ConnectedProviderRow({"name": "openai", "display_name": "OpenAI", "has_key": True})
    captured = []
    row.clicked.connect(lambda k: captured.append(k))
    row._on_click()
    assert captured == ["openai"]


def test_dialog_disconnect_button_and_signal(qapp):
    dialog = ProviderConfigDialog({"name": "openai", "display_name": "OpenAI", "has_key": True})
    assert dialog.btn_disconnect.isHidden() is False
    captured = []
    dialog.remove_requested.connect(lambda k: captured.append(k))
    dialog.btn_disconnect.click()
    assert captured == ["openai"]


def test_dialog_disconnect_hidden_without_saved_key(qapp):
    dialog = ProviderConfigDialog({"name": "openai", "display_name": "OpenAI", "has_key": False})
    assert dialog.btn_disconnect.isHidden() is True


def test_dialog_status_badge(qapp):
    dialog = ProviderConfigDialog({"name": "openai", "display_name": "OpenAI", "has_key": True})
    dialog.set_badge("connected")
    assert "Connected" in dialog.lbl_status.text()
    dialog.set_badge("invalid_key", "bad credentials")
    assert "Invalid key" in dialog.lbl_status.text()
    dialog.set_badge("refreshed", "Refreshed (3 models)")
    assert "Refreshed (3 models)" in dialog.lbl_status.text()


def test_dialog_refresh_emits_key_and_url(qapp):
    dialog = ProviderConfigDialog({
        "name": "custom_openai",
        "display_name": "Custom OpenAI Compatible",
        "has_key": False,
        "base_url": "https://example.com/v1",
    })
    dialog.inp_base_url.setText("https://example.com/v1")
    captured = []
    dialog.refresh_models_requested.connect(lambda p, k, u: captured.append((p, k, u)))
    dialog._on_refresh()
    assert captured == [("custom_openai", "", "https://example.com/v1")]


def test_dialog_save_emits_signature(qapp):
    dialog = ProviderConfigDialog({
        "name": "google_gemini",
        "display_name": "Google Gemini",
        "has_key": False,
    })
    dialog.inp_key.setText("sk-test")
    captured = []
    dialog.save_requested.connect(lambda *args: captured.append(args))
    dialog._on_save(is_default=False)
    assert len(captured) == 1
    assert captured[0][0] == "google_gemini"
    assert captured[0][1] == "sk-test"
