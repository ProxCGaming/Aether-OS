"""Unit test for Settings window provider card states and save state."""
import pytest
from PySide6.QtWidgets import QApplication
from aether_ui.settings_window import ProviderConfigDialog, _MASKED_PLACEHOLDER

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

def test_settings_masked_key_and_save_button_state(qapp):
    gemini_card = ProviderConfigDialog(
        {"name": "google_gemini", "display_name": "Google Gemini", "has_key": True,
         "is_default": True, "models": ["gemini-3-flash-preview"]}
    )
    openrouter_card = ProviderConfigDialog(
        {"name": "openrouter", "display_name": "OpenRouter", "has_key": True,
         "is_default": False, "models": ["liquid/lfm-2.5-2.6b:free"]}
    )
    openai_card = ProviderConfigDialog(
        {"name": "openai", "display_name": "OpenAI", "has_key": False,
         "is_default": False, "models": []}
    )

    # 1. Configured providers should display masked placeholder bullets
    assert gemini_card.inp_key.text() == _MASKED_PLACEHOLDER
    assert gemini_card.has_saved_key is True
    assert gemini_card.btn_save.isEnabled() is False
    assert gemini_card.btn_save.text() == "Saved ✓"

    assert openrouter_card.inp_key.text() == _MASKED_PLACEHOLDER
    assert openrouter_card.has_saved_key is True
    assert openrouter_card.btn_save.isEnabled() is False

    assert openai_card.inp_key.text() == ""
    assert openai_card.has_saved_key is False
    assert openai_card.btn_save.isEnabled() is False

    # 2. Modifying a key should re-enable the Save button
    openai_card.inp_key.setText("sk-test123456")
    assert openai_card.btn_save.isEnabled() is True
    assert openai_card.btn_save.text() == "Save"

    # 3. Model click -> Default emit
    event_emitted = False

    def on_save_req(p, k, is_def, m, url, dn, pt):
        nonlocal event_emitted
        assert p == "openrouter"
        assert is_def is True
        assert m == "liquid/lfm-2.5-2.6b:free"
        event_emitted = True

    openrouter_card.save_requested.connect(on_save_req)
    openrouter_card.chk_default.setChecked(True)

    if openrouter_card.model_list.count() > 0:
        item = openrouter_card.model_list.item(0)
        openrouter_card._on_model_clicked(item)

    assert event_emitted is True

    # 4. Reveal / Hide toggle
    assert openrouter_card.btn_show_key.text() == "⬡ Reveal"
    openrouter_card.btn_show_key.click()
    assert openrouter_card.btn_show_key.text() == "✕ Hide"
    openrouter_card.btn_show_key.click()
    assert openrouter_card.btn_show_key.text() == "⬡ Reveal"
