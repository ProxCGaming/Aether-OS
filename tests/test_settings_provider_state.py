"""Unit test for Settings window provider card states, default mutual exclusion, and save state."""
import pytest
from PySide6.QtWidgets import QApplication
from aether_ui.settings_window import AetherConfigWindow, ProviderAccordionCard, _MASKED_PLACEHOLDER

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

def test_settings_masked_key_and_save_button_state(qapp):
    win = AetherConfigWindow()
    
    # Populate with 2 providers (one with key, one without)
    sample_providers = [
        {"name": "google_gemini", "has_key": True, "is_default": True, "models": ["gemini-3-flash-preview"]},
        {"name": "openrouter", "has_key": True, "is_default": False, "models": ["liquid/lfm-2.5-2.6b:free"]},
        {"name": "openai", "has_key": False, "is_default": False, "models": []},
    ]
    win.populate_providers(sample_providers)
    
    gemini_card = win._provider_cards["google_gemini"]
    openrouter_card = win._provider_cards["openrouter"]
    openai_card = win._provider_cards["openai"]
    
    # 1. Configured providers should display masked placeholder bullets
    assert gemini_card.inp_key.text() == _MASKED_PLACEHOLDER
    assert gemini_card.has_saved_key is True
    assert gemini_card.btn_save.isEnabled() is False
    assert gemini_card.btn_save.text() == "Saved ✓"
    assert gemini_card.chk_default.isChecked() is True
    
    assert openrouter_card.inp_key.text() == _MASKED_PLACEHOLDER
    assert openrouter_card.has_saved_key is True
    assert openrouter_card.btn_save.isEnabled() is False
    assert openrouter_card.chk_default.isChecked() is False
    
    assert openai_card.inp_key.text() == ""
    assert openai_card.has_saved_key is False
    assert openai_card.btn_save.isEnabled() is False
    
    # 2. Modifying a key should re-enable the Save button
    openai_card.inp_key.setText("sk-test123456")
    assert openai_card.btn_save.isEnabled() is True
    assert openai_card.btn_save.text() == "Save"
    
    # 3. Default toggle mutual exclusion
    # Switching default to openrouter should uncheck google_gemini
    openrouter_card.chk_default.click()
    
    assert openrouter_card.chk_default.isChecked() is True
    assert gemini_card.chk_default.isChecked() is False
    
    # 4. Reveal / Hide toggle
    assert openrouter_card.btn_show_key.text() == "⬡ Reveal"
    openrouter_card.btn_show_key.click()
    assert openrouter_card.btn_show_key.text() == "✕ Hide"
    openrouter_card.btn_show_key.click()
    assert openrouter_card.btn_show_key.text() == "⬡ Reveal"
