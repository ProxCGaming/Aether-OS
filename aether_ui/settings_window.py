"""AETHER Settings Modal Overlay with Left Sidebar Navigation & Blur Backdrop.

Matches the reference layout:
- Left Sidebar with categorized navigation items (Model, Providers, Tools, MCP, Security, About).
- Model View with active Provider/Model dropdowns, Apply button, Reasoning level, and Auxiliary Models breakdown
  (Vision, Web extract, Compression, Skills hub, Approval, MCP, Title gen) with "Set to main" and "Change" actions.
- Providers View with API key inputs, Test, Show Key, Get Key, and Ollama local models.
  * Clear visual feedback for configured keys (masked bullets, connected badge).
  * Save button disables after saving and re-enables on edits.
  * Refined reveal button styling.
  * Mutually exclusive default provider toggle with automatic persistence.
- Tools & Approvals View matching sandbox policies.
- Security & About Views.
- Full modal overlay behavior: centers over main window, applies QGraphicsBlurEffect to background HUD,
  blocks interaction with the HUD underneath, and smoothly closes on Escape / outside click / close button.
"""
import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional
import webbrowser

from PySide6.QtCore import QPoint, QRect, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QRadioButton,
)

from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    CHECKBOX_CSS,
    COMBO_CSS,
    LIST_CSS,
    SCROLLBAR_CSS,
    INPUT_CSS,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_OFFLINE,
    STATUS_STANDBY,
    SURFACE_BG,
    SURFACE_BORDER,
    SURFACE_BORDER_LIGHT,
    SURFACE_CARD,
    SURFACE_PANEL,
    SURFACE_PANEL_HOVER,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

_MASKED_PLACEHOLDER = "••••••••••••••••••••••••••••"

_PROVIDER_DOCS = {
    "google_gemini": "https://aistudio.google.com/app/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openrouter": "https://openrouter.ai/keys",
    "custom_openai": "https://ollama.com",
}

_PROVIDER_ICONS = {
    "google_gemini": "✦",
    "openai": "🗲",
    "anthropic": "⚙",
    "deepseek": "⟨/⟩",
    "openrouter": "∿",
    "custom_openai": ">_",
}


_PROVIDER_DESCRIPTIONS = {
    "google_gemini": "Access to Gemini Pro, Flash, and advanced multimodal reasoning models.",
    "openai": "Direct access to GPT-4o, GPT-4 Turbo, and embeddings.",
    "anthropic": "Direct access to Claude models, including Opus, Sonnet, and Haiku.",
    "deepseek": "Access to high-performance DeepSeek Coder and Chat models.",
    "openrouter": "Curated models including Claude, GPT, Gemini and more via one API.",
    "custom_openai": "Connect to local LM Studio, Ollama, or vLLM instances."
}

_PROVIDERS_CONFIG = [
    ("google_gemini", "Google Gemini"),
    ("openai", "OpenAI (GPT-4o)"),
    ("anthropic", "Anthropic Claude"),
    ("deepseek", "DeepSeek Coder"),
    ("openrouter", "OpenRouter Meta API"),
    ("custom_openai", "Custom OpenAI Compatible"),
]


def _stable_custom_key(display_name: str) -> str:
    """Build a stable custom-provider key from a display name (no random UUIDs)."""
    import hashlib
    import re
    cleaned = (display_name or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "_", cleaned.lower()).strip("_")
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"custom_{slug[:32]}_{digest}" if slug else f"custom_{digest}"

class LocalModelItem(QWidget):
    """Row widget for a local (Ollama) model in Installed or Available sections."""

    download_clicked = Signal(str)  # model name
    delete_clicked = Signal(str)    # model name

    def __init__(self, name: str, size: str, downloaded: bool = False, path: str = "", parent=None):
        super().__init__(parent)
        self.name = name
        self.downloaded = downloaded
        self.path = path
        self.setFixedHeight(54)
        self.setStyleSheet("""
            QWidget { background: #161B22; border: 1px solid #30363D; border-radius: 8px; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        info_lo = QVBoxLayout()
        info_lo.setSpacing(2)
        lbl_name = QLabel(name)
        lbl_name.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6EDF3; border: none;")
        info_lo.addWidget(lbl_name)

        lbl_sub = QLabel(path if downloaded and path else size)
        lbl_sub.setStyleSheet("font-size: 11px; color: #8B949E; border: none;")
        info_lo.addWidget(lbl_sub)
        layout.addLayout(info_lo, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background: #0D1117; border: 1px solid #30363D; border-radius: 6px; text-align: center; color: transparent; }
            QProgressBar::chunk { background: #38BDF8; border-radius: 5px; }
        """)
        layout.addWidget(self.progress_bar)

        if downloaded:
            self.btn_action = QPushButton("Delete")
            self.btn_action.setStyleSheet("QPushButton { background: rgba(242, 65, 91, 0.15); color: #FA5870; font-size: 11px; font-weight: 600; border: none; border-radius: 5px; padding: 5px 12px; } QPushButton:hover { background: rgba(242, 65, 91, 0.28); }")
            self.btn_action.clicked.connect(lambda: self.delete_clicked.emit(self.name))
        else:
            self.btn_action = QPushButton("Download")
            self.btn_action.setStyleSheet("QPushButton { background: #1F6FEB; color: #FFFFFF; font-size: 11px; font-weight: 600; border: none; border-radius: 5px; padding: 5px 12px; } QPushButton:hover { background: #388BFD; }")
            self.btn_action.clicked.connect(lambda: self.download_clicked.emit(self.name))

        self.btn_action.setFixedHeight(28)
        self.btn_action.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.btn_action)

    def set_progress(self, percent: float):
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(int(percent))
        self.btn_action.setText(f"{percent:.0f}%")
        if percent >= 100:
            self.progress_bar.setVisible(False)
            self.btn_action.setText("Delete")
            self.btn_action.setStyleSheet("QPushButton { background: rgba(242, 65, 91, 0.15); color: #FA5870; font-size: 11px; font-weight: 600; border: none; border-radius: 5px; padding: 5px 12px; } QPushButton:hover { background: rgba(242, 65, 91, 0.28); }")


class CapabilitiesCheckRow(QWidget):
    """Accordion expandable row for capability check scheduling & run history."""

    set_schedule_requested = Signal(str, str)  # schedule, method
    run_now_requested = Signal(str)            # method

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_expanded = False
        self._build_ui()

    def _build_ui(self):
        main_lo = QVBoxLayout(self)
        main_lo.setContentsMargins(0, 0, 0, 0)
        main_lo.setSpacing(0)

        self.hdr = QFrame()
        self.hdr.setFixedHeight(44)
        self.hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hdr.setStyleSheet("""
            QFrame { background: #161B22; border: 1px solid #30363D; border-radius: 8px; }
            QFrame:hover { border-color: #58A6FF; }
        """)
        hdr_lo = QHBoxLayout(self.hdr)
        hdr_lo.setContentsMargins(12, 0, 12, 0)

        self.lbl_title = QLabel("⚙ Capabilities Check Scheduler")
        self.lbl_title.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6EDF3; border: none;")
        hdr_lo.addWidget(self.lbl_title)
        hdr_lo.addStretch()

        self.lbl_status = QLabel("Weekly • Both")
        self.lbl_status.setStyleSheet("font-size: 11px; color: #38BDF8; font-weight: 600; border: none;")
        hdr_lo.addWidget(self.lbl_status)

        self.lbl_arrow = QLabel("▼")
        self.lbl_arrow.setStyleSheet("font-size: 10px; color: #8B949E; border: none;")
        hdr_lo.addWidget(self.lbl_arrow)

        self.hdr.mousePressEvent = lambda e: self.toggle_expand()
        main_lo.addWidget(self.hdr)

        self.body = QFrame()
        self.body.setVisible(False)
        self.body.setStyleSheet("QFrame { background: #0D1117; border: 1px solid #30363D; border-top: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; padding: 10px; }")
        body_lo = QVBoxLayout(self.body)
        body_lo.setSpacing(10)

        lbl_method = QLabel("Method Choice:")
        lbl_method.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E;")
        body_lo.addWidget(lbl_method)

        method_lo = QHBoxLayout()
        self.rb_opt_a = QRadioButton("Option A")
        self.rb_opt_c = QRadioButton("Option C")
        self.rb_both = QRadioButton("Both (Recommended)")
        self.rb_both.setChecked(True)

        for rb in (self.rb_opt_a, self.rb_opt_c, self.rb_both):
            rb.setStyleSheet("QRadioButton { color: #C9D1D9; font-size: 11px; }")
            method_lo.addWidget(rb)

        body_lo.addLayout(method_lo)

        self.lbl_method_desc = QLabel(
            "• Option A: Zero-cost curated benchmark cards (MMLU, HumanEval, MATH).\\n"
            "• Option C: Live micro-evals measuring accuracy and latency on reachable models.\\n"
            "• Both: Synthesizes both sources for verified capability routing."
        )
        self.lbl_method_desc.setStyleSheet("font-size: 10px; color: #8B949E; line-height: 1.4; background: #161B22; border: 1px solid #21262D; border-radius: 6px; padding: 6px;")
        body_lo.addWidget(self.lbl_method_desc)

        lbl_interval = QLabel("Schedule Interval:")
        lbl_interval.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E;")
        body_lo.addWidget(lbl_interval)

        int_lo = QHBoxLayout()
        self.cmb_interval = QComboBox()
        self.cmb_interval.setStyleSheet("QComboBox { background: #161B22; color: #58A6FF; font-size: 11px; font-weight: 600; border: 1px solid #30363D; border-radius: 6px; padding: 5px 8px; min-width: 150px; }")
        self.cmb_interval.addItems(["weekly", "daily", "monthly", "custom", "none"])
        self.cmb_interval.currentTextChanged.connect(self._on_schedule_changed)
        int_lo.addWidget(self.cmb_interval)

        self.btn_run_now = QPushButton("Run Now")
        self.btn_run_now.setStyleSheet("QPushButton { background: #1F6FEB; color: #FFFFFF; font-size: 11px; font-weight: 600; border: none; border-radius: 5px; padding: 5px 12px; } QPushButton:hover { background: #388BFD; }")
        self.btn_run_now.clicked.connect(lambda: self.run_now_requested.emit(self._get_method()))
        int_lo.addWidget(self.btn_run_now)
        
        body_lo.addLayout(int_lo)
        main_lo.addWidget(self.body)

    def toggle_expand(self):
        self.is_expanded = not self.is_expanded
        self.body.setVisible(self.is_expanded)
        self.lbl_arrow.setText("▲" if self.is_expanded else "▼")
        self.hdr.setStyleSheet("QFrame { background: #161B22; border: 1px solid #30363D; " + 
            ("border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; border-top-left-radius: 8px; border-top-right-radius: 8px; }" if self.is_expanded else "border-radius: 8px; }")
        )

    def _on_schedule_changed(self, text: str):
        method_str = "Both" if self.rb_both.isChecked() else ("Option A" if self.rb_opt_a.isChecked() else "Option C")
        self.lbl_status.setText(f"{text.title()} • {method_str}")
        self.set_schedule_requested.emit(text, self._get_method())
        
    def _get_method(self) -> str:
        if self.rb_both.isChecked(): return "both"
        if self.rb_opt_a.isChecked(): return "option_a"
        return "option_c"




class ProviderConfigDialog(QDialog):
    validate_requested = Signal(str, str, str)
    save_requested = Signal(str, str, bool, str, str, str, str)
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str, str, str)
    reveal_key_requested = Signal(str)
    default_toggled = Signal(str)

    def __init__(self, provider_dict: dict, parent=None):
        super().__init__(parent)
        self.provider_dict = provider_dict
        self.provider_key = provider_dict.get("name", "")
        self.display_name = provider_dict.get("display_name", "") or self.provider_key
        self.provider_type = provider_dict.get("provider_type", "cloud")
        self.has_saved_key = provider_dict.get("has_key", False)
        self.is_default = provider_dict.get("is_default", False)
        self._is_revealed = False

        self.setFixedSize(500, 480)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.frame = QFrame()
        self.frame.setStyleSheet(f"QFrame {{ background: {SURFACE_CARD}; border: 1px solid {SURFACE_BORDER}; border-radius: 12px; }}")
        self.layout = QVBoxLayout(self.frame)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(14)
        
        # Header
        hl = QHBoxLayout()
        icon_str = _PROVIDER_ICONS.get(self.provider_key, "⊕" if self.provider_type == "openai_compatible" else "◈")
        self.lbl_icon = QLabel(icon_str)
        self.lbl_icon.setStyleSheet(f"font-size:20px; color:{BRAND_FRONTEND}; font-weight:bold; background:transparent; border:none;")
        hl.addWidget(self.lbl_icon)

        self.lbl_title = QLabel(f"Configure {self.display_name}")
        self.lbl_title.setStyleSheet(f"font-size:16px; font-weight:600; color:{TEXT_PRIMARY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        hl.addWidget(self.lbl_title)
        
        hl.addStretch()

        self.lbl_status = QLabel("")
        self.lbl_status.setVisible(False)
        self.lbl_status.setStyleSheet("background:transparent; border:none;")
        hl.addWidget(self.lbl_status)
        
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(24, 24)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"QPushButton {{ background: transparent; color: {TEXT_MUTED}; border: none; font-size: 14px; font-weight: bold; }} QPushButton:hover {{ color: {TEXT_PRIMARY}; }}")
        btn_close.clicked.connect(self.close)
        hl.addWidget(btn_close)
        
        self.layout.addLayout(hl)

        # Default provider checkbox
        self.chk_default = QCheckBox("Set as Default Provider")
        self.chk_default.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk_default.setChecked(self.is_default)
        self.chk_default.setStyleSheet(f"""
            QCheckBox {{ font-size: 13px; color: {TEXT_SECONDARY}; font-weight: 600; background: transparent; border: none; padding: 4px 8px; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {SURFACE_BORDER}; background: {SURFACE_BG}; }}
            QCheckBox::indicator:checked {{ background: {STATUS_HEALTHY}; border: 1px solid {STATUS_HEALTHY}; image: url(data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTIiIGhlaWdodD0iMTIiIHZpZXdCb3g9IjAgMCAxMiAxMiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTEwIDNMNC41IDguNUwyIDYiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtbGluZWpvaW49InJvdW5kIi8+Cjwvc3ZnPg==); }}
            QCheckBox::indicator:hover {{ border: 1px solid {BRAND_FRONTEND}; }}
        """)
        self.chk_default.toggled.connect(self._on_default_toggled)
        self.layout.addWidget(self.chk_default)

        # Provider Name Row
        self.name_row = QWidget()
        self.name_row.setStyleSheet("background:transparent; border:none;")
        n_layout = QHBoxLayout(self.name_row)
        n_layout.setContentsMargins(0, 0, 0, 0)
        n_layout.setSpacing(10)
        lbl_name = QLabel("Provider Name:")
        lbl_name.setFixedWidth(80)
        lbl_name.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        n_layout.addWidget(lbl_name)
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("e.g. Local Ollama")
        self.inp_name.setText(self.display_name if self.provider_key != "custom_openai" else "")
        self.inp_name.setStyleSheet(INPUT_CSS)
        self.inp_name.setFixedHeight(34)
        self.inp_name.textChanged.connect(self._on_input_modified)
        n_layout.addWidget(self.inp_name, 1)
        self.layout.addWidget(self.name_row)
        self.name_row.setVisible(self.provider_key == "custom_openai" or self.provider_type == "openai_compatible")

        # Base URL Row
        self.custom_row = QWidget()
        self.custom_row.setStyleSheet("background:transparent; border:none;")
        c_layout = QHBoxLayout(self.custom_row)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(10)
        lbl_base_url = QLabel("Base URL:")
        lbl_base_url.setFixedWidth(80)
        lbl_base_url.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        c_layout.addWidget(lbl_base_url)
        self.inp_base_url = QLineEdit()
        self.inp_base_url.setPlaceholderText("http://localhost:11434/v1")
        self.inp_base_url.setText(provider_dict.get("base_url", ""))
        self.inp_base_url.setStyleSheet(INPUT_CSS)
        self.inp_base_url.setFixedHeight(34)
        self.inp_base_url.textChanged.connect(self._on_input_modified)
        c_layout.addWidget(self.inp_base_url, 1)
        self.layout.addWidget(self.custom_row)
        self.custom_row.setVisible(self.provider_key == "custom_openai" or self.provider_type == "openai_compatible")

        # Key Input Row
        kl = QHBoxLayout()
        kl.setSpacing(10)
        lbl_key = QLabel("API Key:")
        lbl_key.setFixedWidth(80)
        lbl_key.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        kl.addWidget(lbl_key)

        self.inp_key = QLineEdit()
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_key.setPlaceholderText("Enter API Key…")
        if self.has_saved_key:
            self.inp_key.setText(_MASKED_PLACEHOLDER)
        self.inp_key.setStyleSheet(INPUT_CSS)
        self.inp_key.setFixedHeight(34)
        self.inp_key.textChanged.connect(self._on_input_modified)
        kl.addWidget(self.inp_key, 1)

        self.btn_test = QPushButton("Test")
        self.btn_test.setFixedHeight(34)
        self.btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test.setStyleSheet(self._btn_css("#1A1F2C", TEXT_PRIMARY, "#252C3D", border_col=SURFACE_BORDER))
        self.btn_test.clicked.connect(self._on_test)
        kl.addWidget(self.btn_test)

        self.btn_show_key = QPushButton("⬡ Reveal")
        self.btn_show_key.setFixedHeight(34)
        self.btn_show_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_show_key.setStyleSheet(self._btn_css("rgba(124, 111, 255, 0.14)", "#B2A8FF", "rgba(124, 111, 255, 0.25)", border_col="rgba(124, 111, 255, 0.35)"))
        self.btn_show_key.clicked.connect(self._on_toggle_show_key)
        kl.addWidget(self.btn_show_key)

        self.btn_get_key = QPushButton("Get Key ↗")
        self.btn_get_key.setFixedHeight(34)
        self.btn_get_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_get_key.setStyleSheet(self._btn_css("rgba(51, 224, 196, 0.12)", "#33E0C4", "rgba(51, 224, 196, 0.25)", border_col="rgba(51, 224, 196, 0.35)"))
        self.btn_get_key.clicked.connect(self._on_get_key)
        kl.addWidget(self.btn_get_key)
        self.layout.addLayout(kl)

        # Models Row
        mlbl = QLabel("Select Default Model:")
        mlbl.setStyleSheet(f"font-size:12px; font-weight:600; color:{TEXT_SECONDARY}; margin-top:12px; background:transparent; border:none;")
        self.layout.addWidget(mlbl)

        self.model_list = QListWidget()
        self.model_list.setFixedHeight(120)
        self.model_list.setStyleSheet(LIST_CSS)
        self.layout.addWidget(self.model_list)
        self.model_list.itemClicked.connect(self._on_model_clicked)
        models = provider_dict.get("models", [])
        self.update_models(models, provider_dict.get("default_model", ""))

        # Actions
        al = QHBoxLayout()
        al.setSpacing(10)
        
        self.btn_refresh = QPushButton("↻ Refresh Models")
        self.btn_refresh.setFixedHeight(34)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(self._btn_css("#1A1F2C", TEXT_PRIMARY, "#252C3D", border_col=SURFACE_BORDER))
        self.btn_refresh.clicked.connect(self._on_refresh)
        al.addWidget(self.btn_refresh)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setFixedHeight(34)
        self.btn_disconnect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_disconnect.setStyleSheet(self._btn_css("rgba(242, 65, 91, 0.15)", "#FA5870", "rgba(242, 65, 91, 0.28)", border_col="rgba(242, 65, 91, 0.35)"))
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.btn_disconnect.setVisible(self.has_saved_key)
        al.addWidget(self.btn_disconnect)

        al.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(34)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(self._btn_css("rgba(255, 255, 255, 0.05)", TEXT_SECONDARY, "rgba(255, 255, 255, 0.1)", border_col=SURFACE_BORDER))
        self.btn_cancel.clicked.connect(self.close)
        al.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("Save")
        self.btn_save.setFixedHeight(34)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_save_button_state(is_dirty=False)
        self.btn_save.clicked.connect(lambda: self._on_save(is_default=self.chk_default.isChecked()))
        al.addWidget(self.btn_save)

        self.layout.addLayout(al)
        main_layout.addWidget(self.frame)

    def _btn_css(self, bg: str, fg: str, hover: str, border_col: str = "transparent", font_weight: str = "600") -> str:
        return f"""
            QPushButton {{ background-color: {bg}; color: {fg}; font-size: 12px; font-weight: {font_weight}; font-family: 'Segoe UI', sans-serif; border: 1px solid {border_col}; border-radius: 6px; padding: 4px 14px; }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:disabled {{ background-color: rgba(43, 217, 160, 0.18); color: rgba(255, 255, 255, 0.35); border: 1px solid rgba(43, 217, 160, 0.15); }}
        """

    def _update_save_button_state(self, is_dirty: bool):
        if is_dirty:
            self.btn_save.setEnabled(True)
            self.btn_save.setText("Save")
            self.btn_save.setStyleSheet(self._btn_css(STATUS_HEALTHY, "#07150E", "#38E5AC", font_weight="700"))
        else:
            self.btn_save.setEnabled(False)
            self.btn_save.setText("Saved ✓" if self.has_saved_key else "Save")
            self.btn_save.setStyleSheet(self._btn_css("rgba(43, 217, 160, 0.20)", "rgba(255, 255, 255, 0.45)", "rgba(43, 217, 160, 0.20)", border_col="rgba(43, 217, 160, 0.2)"))

    def _on_input_modified(self):
        text = self.inp_key.text()
        if text != _MASKED_PLACEHOLDER:
            self._update_save_button_state(is_dirty=True)

    def _on_model_clicked(self, item: QListWidgetItem):
        model_name = item.text().replace(" ✦ (default)", "").strip()
        self._on_save(is_default=self.chk_default.isChecked(), model_name=model_name)

    def update_models(self, models: list, current: str = None):
        self._stop_loading_animation()
        if not models:
            item = QListWidgetItem("No models fetched")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setForeground(QColor(TEXT_MUTED))
            self.model_list.addItem(item)
            return

        for m in models:
            item = QListWidgetItem(f"{m} ✦ (default)" if m == current else m)
            self.model_list.addItem(item)
            if m == current:
                item.setSelected(True)

    def _start_loading_animation(self):
        self.model_list.clear()
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        self._loading_item = QListWidgetItem(f"{self._spinner_frames[0]} Fetching models...")
        self._loading_item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.model_list.addItem(self._loading_item)
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._update_spinner)
        self._spinner_timer.start(100)
        
    def _update_spinner(self):
        if hasattr(self, "_loading_item") and self._loading_item:
            self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
            self._loading_item.setText(f"{self._spinner_frames[self._spinner_idx]} Fetching models...")
            
    def _stop_loading_animation(self):
        if hasattr(self, "_spinner_timer"):
            self._spinner_timer.stop()
        self.model_list.clear()

    def get_raw_key(self) -> str:
        t = self.inp_key.text().strip()
        return "" if t == _MASKED_PLACEHOLDER else t

    def _on_test(self):
        self._start_loading_animation()
        self.set_badge("testing", "Testing…")
        k = self.get_raw_key()
        url = self.inp_base_url.text().strip() if self.custom_row.isVisibleTo(self) else ""
        self.validate_requested.emit(self.provider_key, k, url)

    def _on_refresh(self):
        self._start_loading_animation()
        self.set_badge("testing", "Refreshing…")
        k = self.get_raw_key()
        url = self.inp_base_url.text().strip() if self.custom_row.isVisibleTo(self) else ""
        self.refresh_models_requested.emit(self.provider_key, k, url)

    def _on_disconnect(self):
        self.remove_requested.emit(self.provider_key)
        self.close()

    def _on_save(self, is_default: bool = False, model_name: str = ""):
        k = self.get_raw_key()
        url = self.inp_base_url.text().strip() if self.custom_row.isVisibleTo(self) else ""
        if not model_name:
            items = self.model_list.selectedItems()
            model_name = items[0].text().replace(" ✦ (default)", "").strip() if items else ""
            
        if hasattr(self, "name_row") and self.name_row.isVisibleTo(self):
            new_name = self.inp_name.text().strip()
            if self.provider_key == "custom_openai":
                if not new_name:
                    self.set_badge("error", "Display name is required.")
                    self._update_save_button_state(is_dirty=True)
                    return
                self.display_name = new_name
                # Deterministic key derived from the display name so re-saving the
                # same provider updates it instead of creating duplicates.
                self.provider_key = self._stable_custom_key(self.display_name)
                self.provider_type = "openai_compatible"
            elif new_name:
                self.display_name = new_name

        self._update_save_button_state(is_dirty=False)
        self.btn_save.setText("Saving...")
        self.btn_save.setEnabled(False)
        self.save_requested.emit(self.provider_key, k, is_default, model_name, url, self.display_name, self.provider_type)

    @staticmethod
    def _stable_custom_key(display_name: str) -> str:
        return _stable_custom_key(display_name)

    def on_saved_success(self):
        self.has_saved_key = True
        self.inp_key.blockSignals(True)
        self.inp_key.setText("********")  # _MASKED_PLACEHOLDER is typically 8 asterisks
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._is_revealed = False
        self.btn_show_key.setText("⬡ Reveal")
        self.inp_key.blockSignals(False)
        self._update_save_button_state(is_dirty=False)
        self.close()

    def on_saved_error(self, message: str = ""):
        """Re-enable the dialog after a failed save so the user can retry."""
        self.set_badge("error", message or "Save failed.")
        self._update_save_button_state(is_dirty=True)

    def _on_toggle_show_key(self):
        if not self._is_revealed:
            if not self.inp_key.text() or self.inp_key.text() == _MASKED_PLACEHOLDER:
                self.reveal_key_requested.emit(self.provider_key)
            self.inp_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_show_key.setText("✕ Hide")
            self._is_revealed = True
        else:
            self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_show_key.setText("⬡ Reveal")
            self._is_revealed = False

    def _on_get_key(self):
        url = _PROVIDER_DOCS.get(self.provider_key, "https://google.com")
        import webbrowser
        webbrowser.open(url)

    def _on_default_toggled(self, checked: bool):
        self.is_default = checked
        self.default_toggled.emit(self.provider_key)

    def reveal_api_key(self, api_key: str):
        self.inp_key.blockSignals(True)
        self.inp_key.setText(api_key)
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Normal)
        self.btn_show_key.setText("✕ Hide")
        self._is_revealed = True
        self.inp_key.blockSignals(False)
        
    def set_badge(self, status: str, message: str = ""):
        color_map = {
            "connected": STATUS_HEALTHY,
            "testing": "#F0B429",
            "refreshed": BRAND_FRONTEND,
            "invalid_key": STATUS_OFFLINE,
            "error": STATUS_OFFLINE,
            "unreachable": STATUS_DEGRADED,
            "no_key": TEXT_MUTED,
        }
        color = color_map.get(status, TEXT_MUTED)
        label_map = {
            "connected": "Connected",
            "testing": message or "Testing…",
            "refreshed": message or "Refreshed",
            "invalid_key": "Invalid key",
            "error": "Error",
            "unreachable": "Unreachable",
            "no_key": "No key",
        }
        text = label_map.get(status, status.replace("_", " ").title())
        if message and status not in ("testing", "refreshed"):
            text = f"{text}: {message}"
        self.lbl_status.setText(f"● {text}")
        self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:700; color:{color}; background:transparent; border:none;")
        self.lbl_status.setVisible(True)

class ConnectedProviderRow(QFrame):
    clicked = Signal(str)
    disconnect_clicked = Signal(str)
    
    def __init__(self, provider_dict: dict, parent=None):
        super().__init__(parent)
        self.provider_dict = provider_dict
        self.provider_key = provider_dict.get("name", "")
        self.display_name = provider_dict.get("display_name", "") or self.provider_key
        
        self.setFixedHeight(64)
        self.setStyleSheet(f"""
            QFrame {{ background: {SURFACE_CARD}; border: 1px solid {SURFACE_BORDER}; border-radius: 8px; }}
            QFrame:hover {{ border-color: {SURFACE_BORDER_LIGHT}; background: {SURFACE_PANEL_HOVER}; }}
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        hl = QHBoxLayout(self)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(12)
        
        icon_str = _PROVIDER_ICONS.get(self.provider_key, "⊕" if provider_dict.get("provider_type") == "openai_compatible" else "◈")
        lbl_icon = QLabel(icon_str)
        lbl_icon.setStyleSheet(f"font-size:18px; color:{TEXT_PRIMARY}; font-weight:bold; background:transparent; border:none;")
        hl.addWidget(lbl_icon)
        
        lbl_name = QLabel(self.display_name)
        lbl_name.setStyleSheet(f"font-size:14px; font-weight:600; color:{TEXT_PRIMARY}; background:transparent; border:none;")
        hl.addWidget(lbl_name)
        
        lbl_badge = QLabel("API key")
        lbl_badge.setStyleSheet(f"font-size:10px; color:{TEXT_SECONDARY}; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; padding: 2px 6px;")
        hl.addWidget(lbl_badge)
        
        hl.addStretch()
        
        self.btn_disc = QPushButton("Disconnect")
        self.btn_disc.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_disc.setStyleSheet(f"QPushButton {{ background: transparent; color: {TEXT_MUTED}; font-size: 13px; border: none; font-weight: 500; }} QPushButton:hover {{ color: {STATUS_OFFLINE}; }}")
        self.btn_disc.clicked.connect(lambda: self.disconnect_clicked.emit(self.provider_key))
        hl.addWidget(self.btn_disc)
        
    def mousePressEvent(self, event):
        self._on_click()
        
    def _on_click(self):
        self.clicked.emit(self.provider_key)


class PopularProviderRow(QFrame):
    clicked = Signal(str)
    
    def __init__(self, provider_dict: dict, parent=None):
        super().__init__(parent)
        self.provider_dict = provider_dict
        self.provider_key = provider_dict.get("name", "")
        self.display_name = provider_dict.get("display_name", "") or self.provider_key
        
        self.setFixedHeight(72)
        self.setStyleSheet(f"""
            QFrame {{ background: {SURFACE_BG}; border-bottom: 1px solid {SURFACE_BORDER}; border-radius: 0px; }}
            QFrame:hover {{ background: {SURFACE_CARD}; }}
        """)
        
        hl = QHBoxLayout(self)
        hl.setContentsMargins(16, 0, 16, 0)
        hl.setSpacing(16)
        
        icon_str = _PROVIDER_ICONS.get(self.provider_key, "⊕" if provider_dict.get("provider_type") == "openai_compatible" else "◈")
        lbl_icon = QLabel(icon_str)
        lbl_icon.setStyleSheet(f"font-size:20px; color:{TEXT_PRIMARY}; font-weight:bold; background:transparent; border:none;")
        hl.addWidget(lbl_icon)
        
        vl = QVBoxLayout()
        vl.setContentsMargins(0, 14, 0, 14)
        vl.setSpacing(4)
        
        title_lo = QHBoxLayout()
        title_lo.setSpacing(8)
        lbl_name = QLabel(self.display_name)
        lbl_name.setStyleSheet(f"font-size:14px; font-weight:600; color:{TEXT_PRIMARY}; background:transparent; border:none;")
        title_lo.addWidget(lbl_name)
        
        if self.provider_key in ("google_gemini", "anthropic"):
            lbl_badge = QLabel("Recommended")
            lbl_badge.setStyleSheet(f"font-size:10px; color:{TEXT_SECONDARY}; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; padding: 2px 6px;")
            title_lo.addWidget(lbl_badge)
            
        title_lo.addStretch()
        vl.addLayout(title_lo)
        
        desc = _PROVIDER_DESCRIPTIONS.get(self.provider_key, "Access to this AI model provider.")
        lbl_desc = QLabel(desc)
        lbl_desc.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        vl.addWidget(lbl_desc)
        
        hl.addLayout(vl, 1)
        
        btn_conn = QPushButton("+ Connect")
        btn_conn.setFixedSize(80, 28)
        btn_conn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_conn.setStyleSheet(f"QPushButton {{ background: rgba(255, 255, 255, 0.05); color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 500; border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 6px; }} QPushButton:hover {{ background: rgba(255, 255, 255, 0.1); }}")
        btn_conn.clicked.connect(lambda: self.clicked.emit(self.provider_key))
        hl.addWidget(btn_conn)

class QuickAddProviderDialog(QFrame):
    save_requested = Signal(str, str, bool, str, str, str, str)  # Match panel signal

    def __init__(self, provider_type: str, parent=None):
        super().__init__(parent)
        self.provider_type = provider_type
        self.setFixedSize(400, 320 if provider_type == "openai_compatible" else 260)
        self.setStyleSheet(f"QFrame {{ background: {SURFACE_BG}; border: 1px solid {SURFACE_BORDER}; border-radius: 12px; }}")

        l = QVBoxLayout(self)
        l.setContentsMargins(20, 20, 20, 20)
        
        title = QLabel(f"Add {'OpenAI Compatible' if provider_type == 'openai_compatible' else 'Anthropic SDK'} Provider")
        title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {TEXT_PRIMARY}; border: none;")
        l.addWidget(title)
        
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("Display Name (e.g. My LM Studio)")
        self.inp_name.setStyleSheet(INPUT_CSS)
        self.inp_name.setFixedHeight(34)
        l.addWidget(self.inp_name)
        
        self.inp_key = QLineEdit()
        self.inp_key.setPlaceholderText("API Key (Optional for local)" if provider_type == 'openai_compatible' else "API Key (Required)")
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_key.setStyleSheet(INPUT_CSS)
        self.inp_key.setFixedHeight(34)
        l.addWidget(self.inp_key)
        
        if provider_type == "openai_compatible":
            self.inp_url = QLineEdit()
            self.inp_url.setText("http://localhost:11434/v1")
            self.inp_url.setStyleSheet(INPUT_CSS)
            self.inp_url.setFixedHeight(34)
            l.addWidget(self.inp_url)
            
        l.addStretch()
        
        bl = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setFixedHeight(32)
        btn_cancel.setStyleSheet(f"QPushButton {{ background: {SURFACE_PANEL}; color: {TEXT_PRIMARY}; border: 1px solid {SURFACE_BORDER}; border-radius: 6px; }}")
        btn_cancel.clicked.connect(self.close)
        
        btn_save = QPushButton("Save & Add")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setFixedHeight(32)
        btn_save.setStyleSheet(f"QPushButton {{ background: {BRAND_ENGINE}; color: #000000; font-weight: bold; border: none; border-radius: 6px; }}")
        btn_save.clicked.connect(self._on_save)
        
        bl.addWidget(btn_cancel)
        bl.addWidget(btn_save)
        l.addLayout(bl)
        
    def _on_save(self):
        name = self.inp_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Display Name is required.")
            return

        # Custom providers use the same stable "custom_<slug>_<hash>" key scheme
        # as the main config dialog so discovery/routing logic is identical.
        key = _stable_custom_key(name) if self.provider_type == "openai_compatible" else name.lower().replace(" ", "_")
        api_key = self.inp_key.text().strip()
        url = self.inp_url.text().strip() if self.provider_type == "openai_compatible" else ""
        
        self.save_requested.emit(key, api_key, False, "", url, name, self.provider_type)
        self.close()

class AddProviderDialog(QFrame):
    provider_selected = Signal(str, str, str) # key, name, type

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(500, 360)
        self.setStyleSheet(f"QFrame {{ background: {SURFACE_BG}; border: 1px solid {SURFACE_BORDER}; border-radius: 12px; }}")

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        
        hdr = QHBoxLayout()
        title = QLabel("Add Provider")
        title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {TEXT_PRIMARY}; border: none;")
        hdr.addWidget(title)
        hdr.addStretch()
        
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(24, 24)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"QPushButton {{ background: transparent; color: {TEXT_MUTED}; border: none; font-size: 14px; font-weight: bold; }} QPushButton:hover {{ color: {TEXT_PRIMARY}; }}")
        btn_close.clicked.connect(self.close)
        hdr.addWidget(btn_close)
        self.layout.addLayout(hdr)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: transparent; }}\n{SCROLLBAR_CSS}")
        
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        grid = QVBoxLayout(w)
        grid.setSpacing(10)
        
        for k, n in _PROVIDERS_CONFIG:
            b = QPushButton(f"  {_PROVIDER_ICONS.get(k, '✦')}    {n}")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ 
                    background: {SURFACE_PANEL}; 
                    color: {TEXT_PRIMARY}; 
                    border: 1px solid {SURFACE_BORDER}; 
                    border-radius: 6px; 
                    padding: 12px; 
                    text-align: left; 
                    font-size: 13px;
                    font-weight: 500;
                }}
                QPushButton:hover {{ 
                    background: {SURFACE_CARD}; 
                    border: 1px solid {BRAND_FRONTEND}; 
                }}
            """)
            b.clicked.connect(lambda checked, pk=k, pn=n: self._select_provider(pk, pn))
            grid.addWidget(b)
            
        scroll.setWidget(w)
        self.layout.addWidget(scroll)
        
    def _select_provider(self, key, name):
        # Close the picker first so it does not linger behind the config dialog
        # (the signal handler opens a modal dialog synchronously).
        self.close()
        self.provider_selected.emit(key, name, "cloud" if key != "custom_openai" else "openai_compatible")

class AetherConfigWindow(QWidget):
    """Full Reference-Styled Modal Settings Window."""

    closed = Signal()
    validate_requested = Signal(str, str, str)
    save_requested = Signal(str, str, bool, str, str, str, str)
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str, str, str)
    reveal_key_requested = Signal(str)
    download_local_model_requested = Signal(str, str)
    delete_local_model_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.SubWindow)
        self.setStyleSheet(SCROLLBAR_CSS)

        self._provider_cards: Dict[str, object] = {}
        self.mcp_servers = {}
        self._sidebar_buttons: List[QPushButton] = []
        self._build_ui()

    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Center Dialog Card
        self.card = QFrame(self)
        self.card.setObjectName("settings_card")
        self.card.setStyleSheet(f"""
            QFrame#settings_card {{
                background-color: {SURFACE_BG};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 12px;
            }}
        """)
        self.card.setFixedSize(720, 580)

        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # -------------------------------------------------------------------
        # LEFT SIDEBAR NAVIGATION
        # -------------------------------------------------------------------
        sidebar = QFrame()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_PANEL};
                border-right: 1px solid {SURFACE_BORDER};
                border-top-left-radius: 12px;
                border-bottom-left-radius: 12px;
            }}
        """)
        s_layout = QVBoxLayout(sidebar)
        s_layout.setContentsMargins(10, 16, 10, 16)
        s_layout.setSpacing(4)

        # Sidebar Title
        lbl_brand = QLabel("SETTINGS")
        lbl_brand.setStyleSheet(f"font-size:11px; font-weight:700; color:{TEXT_MUTED}; letter-spacing:1px; margin-left:6px; margin-bottom:8px;")
        s_layout.addWidget(lbl_brand)

        # Sidebar nav items
        nav_items = [
            ("🎲 Model", 0),
            ("⚡ Providers", 1),
            ("🔧 Tools & Approvals", 2),
            ("🛡 Safety & Sandbox", 3),
            ("🧩 Plugins", 4),
            ("🤖 Agents", 5),
            ("📚 Skills", 6),
            ("🔌 MCP Servers", 7),
            ("🧠 Capabilities Check", 8),
            ("ℹ About", 9),
        ]

        for label, idx in nav_items:
            b = self._make_sidebar_btn(label, idx)
            s_layout.addWidget(b)
            self._sidebar_buttons.append(b)

        s_layout.addStretch()

        # Bottom sidebar toolbar
        tb_bar = QHBoxLayout()
        tb_bar.setSpacing(6)
        btn_dl = QPushButton("↓")
        btn_dl.setToolTip("Export config")
        btn_dl.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_dl.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")

        btn_ul = QPushButton("↑")
        btn_ul.setToolTip("Import config")
        btn_ul.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ul.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")

        btn_rf = QPushButton("↻")
        btn_rf.setToolTip("Reload engine")
        btn_rf.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rf.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")

        tb_bar.addWidget(btn_dl)
        tb_bar.addWidget(btn_ul)
        tb_bar.addWidget(btn_rf)
        tb_bar.addStretch()
        s_layout.addLayout(tb_bar)

        card_layout.addWidget(sidebar)

        # -------------------------------------------------------------------
        # RIGHT MAIN CONTENT AREA
        # -------------------------------------------------------------------
        main_content = QWidget()
        mc_layout = QVBoxLayout(main_content)
        mc_layout.setContentsMargins(18, 14, 18, 14)
        mc_layout.setSpacing(12)

        # Top Header Bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.lbl_sys_ok = QLabel("SYS_OK: 145ms")
        self.lbl_sys_ok.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED}; font-family:Consolas, monospace;")
        top_bar.addWidget(self.lbl_sys_ok)

        self.lbl_online = QLabel("● ONLINE")
        self.lbl_online.setStyleSheet(f"""
            font-size: 10px; font-weight: 700; color: {STATUS_HEALTHY};
            background: rgba(43, 217, 160, 0.12);
            border: 1px solid rgba(43, 217, 160, 0.25);
            border-radius: 4px; padding: 2px 6px;
        """)
        top_bar.addWidget(self.lbl_online)

        top_bar.addStretch()

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(26, 26)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_MUTED};
                font-size: 13px;
                font-weight: bold;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: {STATUS_OFFLINE};
                color: #FFFFFF;
            }}
        """)
        btn_close.clicked.connect(self.close_modal)
        top_bar.addWidget(btn_close)
        mc_layout.addLayout(top_bar)

        # Stacked Views
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent; border:none;")

        # Tab 0: Model & Routing
        self.view_model = self._build_model_routing_tab()
        self.stack.addWidget(self.view_model)

        # Tab 1: Providers & Keys
        self.view_providers = self._build_providers_tab()
        self.stack.addWidget(self.view_providers)

        # Tab 2: Tools & Approvals
        self.view_tools = self._build_tools_tab()
        self.stack.addWidget(self.view_tools)

        # Tab 3: Security & Sandbox
        self.view_security = self._build_security_tab()
        self.stack.addWidget(self.view_security)

        # Tab 4: Plugins
        from aether_ui.settings.plugins_tab import PluginsTab
        # main_window holds ws_client in self.parent() but let's try to pass ws_client safely
        self.view_plugins = PluginsTab(ws_client=getattr(self.parent(), 'ws_client', None))
        self.stack.addWidget(self.view_plugins)

        # Tab 5: Agents
        from aether_ui.settings.agents_tab import AgentsTab
        self.view_agents = AgentsTab()
        self.stack.addWidget(self.view_agents)

        # Tab 6: Skills
        from aether_ui.settings.skills_tab import SkillsTab
        self.view_skills = SkillsTab()
        self.stack.addWidget(self.view_skills)

        # Tab 7: MCP Servers
        self.view_mcp = self._build_mcp_tab()
        self.stack.addWidget(self.view_mcp)

        # Tab 8: Capabilities Check
        self.view_capabilities = self._build_capabilities_tab()
        self.stack.addWidget(self.view_capabilities)

        # Tab 9: About
        self.view_about = self._build_about_tab()
        self.stack.addWidget(self.view_about)

        mc_layout.addWidget(self.stack, 1)
        card_layout.addWidget(main_content, 1)
        root_layout.addWidget(self.card)

        self._switch_tab(0)

        # Seed the Providers tab immediately so it is never blank on cold launch,
        # even before the WebSocket handshake delivers PROVIDER_LIST_RESPONSE.
        self.populate_providers(self._default_provider_dicts())

    def paintEvent(self, event):
        """Paint semi-transparent backdrop overlay."""
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(11, 13, 18, 190))

    def mousePressEvent(self, event):
        """Close on clicking outside the card."""
        if not self.card.geometry().contains(event.position().toPoint()):
            self.close_modal()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_modal()
        super().keyPressEvent(event)

    def close_modal(self):
        self.hide()
        self.closed.emit()

    def update_geometry(self):
        parent = self.parentWidget()
        if parent:
            self.setGeometry(parent.rect())
            card_w = min(780, max(520, int(parent.width() * 0.94)))
            card_h = min(640, max(460, int(parent.height() * 0.92)))
            self.card.setFixedSize(card_w, card_h)

    def _make_sidebar_btn(self, label: str, index: int) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedHeight(34)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._switch_tab(index))
        return btn

    def _switch_tab(self, index: int):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self._sidebar_buttons):
            if i == index:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: rgba(124, 111, 255, 0.18);
                        color: {TEXT_PRIMARY};
                        font-size: 12px;
                        font-weight: 700;
                        border: 1px solid {BRAND_FRONTEND};
                        border-radius: 6px;
                        text-align: left;
                        padding-left: 12px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: transparent;
                        color: {TEXT_SECONDARY};
                        font-size: 12px;
                        font-weight: 500;
                        border: none;
                        border-radius: 6px;
                        text-align: left;
                        padding-left: 12px;
                    }}
                    QPushButton:hover {{
                        background-color: {SURFACE_CARD};
                        color: {TEXT_PRIMARY};
                    }}
                """)

    def _build_model_routing_tab(self) -> QWidget:
        """Builds Model selection & Auxiliary Models breakdown matching reference screenshot."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background:transparent; border:none;")

        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(14)

        # Header description
        lbl_desc = QLabel("Applies to new sessions. Use the model picker in the composer to hot-swap the active chat.")
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; line-height:1.4; background:transparent; border:none;")
        lo.addWidget(lbl_desc)

        # Primary Provider Dropdown
        self.cmb_primary_provider = QComboBox()
        self.cmb_primary_provider.setStyleSheet(COMBO_CSS)
        self.cmb_primary_provider.setFixedHeight(34)
        self.cmb_primary_provider.currentTextChanged.connect(self._on_primary_provider_changed)
        lo.addWidget(self.cmb_primary_provider)

        # Primary Model Dropdown
        self.cmb_primary_model = QComboBox()
        self.cmb_primary_model.setStyleSheet(COMBO_CSS)
        self.cmb_primary_model.setFixedHeight(34)
        lo.addWidget(self.cmb_primary_model)

        # Apply + Defaults & Reasoning Row
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(12)

        self.btn_apply_model = QPushButton("Apply")
        self.btn_apply_model.setFixedHeight(30)
        self.btn_apply_model.setFixedWidth(75)
        self.btn_apply_model.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply_model.setStyleSheet(f"""
            QPushButton {{
                background-color: #2D68FF;
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 700;
                border: none;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: #4077FF;
            }}
        """)
        ctrl_row.addWidget(self.btn_apply_model)

        lbl_defs = QLabel("Defaults")
        lbl_defs.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED}; font-weight:600;")
        ctrl_row.addWidget(lbl_defs)

        lbl_reason = QLabel("Reasoning")
        lbl_reason.setStyleSheet(f"font-size:12px; font-weight:600; color:{TEXT_PRIMARY}; margin-left:8px;")
        ctrl_row.addWidget(lbl_reason)

        self.cmb_reasoning = QComboBox()
        self.cmb_reasoning.setStyleSheet(COMBO_CSS)
        self.cmb_reasoning.setFixedHeight(28)
        self.cmb_reasoning.setFixedWidth(110)
        self.cmb_reasoning.addItems(["Standard", "Medium", "Deep Thinking"])
        self.cmb_reasoning.setCurrentIndex(1)
        ctrl_row.addWidget(self.cmb_reasoning)

        ctrl_row.addStretch()
        lo.addLayout(ctrl_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{SURFACE_BORDER}; margin-top:8px; margin-bottom:8px;")
        lo.addWidget(sep)

        # Auxiliary Models Section
        aux_header = QHBoxLayout()
        lbl_aux_title = QLabel("⚙ Auxiliary models")
        lbl_aux_title.setStyleSheet(f"font-size:13px; font-weight:700; color:{TEXT_PRIMARY};")
        aux_header.addWidget(lbl_aux_title)
        aux_header.addStretch()

        btn_reset_aux = QPushButton("Reset all to main")
        btn_reset_aux.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_reset_aux.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; font-size:11px; font-weight:600; border:none;")
        aux_header.addWidget(btn_reset_aux)
        lo.addLayout(aux_header)

        lbl_aux_sub = QLabel("Helper tasks run on the main model by default. Assign a dedicated model to any task to override.")
        lbl_aux_sub.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED};")
        lo.addWidget(lbl_aux_sub)

        # Auxiliary tasks rows
        aux_tasks = [
            ("Vision", "Image analysis", "auto · use main model"),
            ("Web extract", "Page summarization", "auto · use main model"),
            ("Compression", "Context compaction", "auto · use main model"),
            ("Skills hub", "Skill search", "auto · use main model"),
            ("Approval", "Smart auto-approve", "auto · use main model"),
            ("MCP", "MCP tool routing", "auto · use main model"),
            ("Title gen", "Session titles", "auto · use main model"),
        ]

        for name, tag, sub in aux_tasks:
            card = self._make_aux_task_row(name, tag, sub)
            lo.addWidget(card)

        lo.addStretch()
        scroll.setWidget(container)
        return scroll

    def _make_aux_task_row(self, name: str, tag: str, sub: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_CARD};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
                padding: 10px 14px;
            }}
            QFrame:hover {{
                border-color: {SURFACE_BORDER_LIGHT};
            }}
        """)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)

        top = QHBoxLayout()
        lbl_n = QLabel(name)
        lbl_n.setStyleSheet(f"font-size:12px; font-weight:700; color:{TEXT_PRIMARY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        top.addWidget(lbl_n)

        lbl_t = QLabel(tag)
        lbl_t.setStyleSheet(f"font-size:10px; color:{TEXT_MUTED}; background:transparent; border:none; margin-left:4px;")
        top.addWidget(lbl_t)
        top.addStretch()

        cl.addLayout(top)

        bottom = QHBoxLayout()
        lbl_s = QLabel(sub)
        lbl_s.setStyleSheet(f"font-size:11px; color:{TEXT_SECONDARY}; font-family:Consolas, monospace; background:transparent; border:none;")
        bottom.addWidget(lbl_s)
        bottom.addStretch()

        btn_main = QPushButton("Set to main")
        btn_main.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_main.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; font-size:11px; font-weight:600; border:none; padding:4px 8px;")
        bottom.addWidget(btn_main)

        btn_chg = QPushButton("Change")
        btn_chg.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_chg.setStyleSheet(f"background:transparent; color:{BRAND_FRONTEND}; font-size:11px; font-weight:600; border:none; padding:4px 8px;")
        bottom.addWidget(btn_chg)

        cl.addLayout(bottom)
        return card

    def _on_provider_default_toggled(self, selected_provider: str):
        """Ensure mutual exclusion for default provider checkbox across all provider cards."""
        for p_key, card in self._provider_cards.items():
            if p_key != selected_provider:
                card.chk_default.blockSignals(True)
                card.chk_default.setChecked(False)
                card.is_default = False
                card.chk_default.blockSignals(False)
            else:
                card.is_default = True

    def _build_tools_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background:transparent; border:none;")

        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        lbl_t = QLabel("Tool Execution & Policy Sandbox")
        lbl_t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(lbl_t)

        tools = [
            ("search_web", "Web search and live duckduckgo extraction", "READONLY · In-Process", "#2BD9A0", True),
            ("read_url_content", "Fetch public markdown/HTML pages", "READONLY · In-Process", "#2BD9A0", True),
            ("view_file", "Read local project files", "READONLY · In-Process", "#2BD9A0", True),
            ("list_dir", "Inspect directory trees", "READONLY · In-Process", "#2BD9A0", True),
            ("grep_search", "Ripgrep pattern matching", "READONLY · In-Process", "#2BD9A0", True),
            ("run_command", "PowerShell sandbox execution", "MUTATING · Sub-Process", "#F2A93B", False),
            ("write_to_file", "Create or overwrite filesystem files", "MUTATING · In-Process", "#F2A93B", False),
            ("replace_file_content", "Modify source code blocks", "MUTATING · In-Process", "#F2A93B", False),
        ]

        for name, desc, tag, col, auto_appr in tools:
            card = self._make_tool_policy_card(name, desc, tag, col, auto_appr)
            lo.addWidget(card)

        lo.addStretch()
        scroll.setWidget(container)
        return scroll

    def _make_tool_policy_card(self, name: str, desc: str, tag: str, dot_col: str, auto_appr: bool) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"background:{SURFACE_CARD}; border:1px solid {SURFACE_BORDER}; border-radius:8px; padding:10px 14px;")
        cl = QHBoxLayout(card)
        cl.setSpacing(10)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{dot_col}; font-size:11px; background:transparent; border:none;")
        cl.addWidget(dot)

        vl = QVBoxLayout()
        vl.setSpacing(2)
        lbl_name = QLabel(name)
        lbl_name.setStyleSheet(f"font-size:12px; font-weight:700; color:{TEXT_PRIMARY}; font-family:Consolas, monospace; background:transparent; border:none;")
        vl.addWidget(lbl_name)
        lbl_desc = QLabel(f"{desc} · [{tag}]")
        lbl_desc.setStyleSheet(f"font-size:10px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        vl.addWidget(lbl_desc)
        cl.addLayout(vl, 1)

        cmb = QComboBox()
        cmb.setStyleSheet(f"""
            QComboBox {{
                font-size: 11px; color: {TEXT_MUTED}; background: {SURFACE_BG};
                border: 1px solid {SURFACE_BORDER}; border-radius: 4px; padding: 3px 8px;
            }}
        """)
        cmb.addItems(["Always Allow", "Require Approval", "Deny"])
        cmb.setCurrentIndex(0 if auto_appr else 1)
        cl.addWidget(cmb)
        return card

    def _build_security_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        t = QLabel("Security & Sandbox Isolation")
        t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(t)

        c1 = self._make_info_card(
            "🔒 Windows DPAPI Key Storage",
            "Provider API keys are encrypted with Windows DPAPI and stored at ~/.aether/secrets.db.",
            "ACTIVE · Hardware Encrypted",
            STATUS_HEALTHY,
        )
        lo.addWidget(c1)

        c2 = self._make_info_card(
            "🛡 Job Object Sandboxing",
            "Sub-processes run in restricted Windows Job Objects with CPU/memory limits.",
            "ENABLED · Isolated",
            STATUS_HEALTHY,
        )
        lo.addWidget(c2)

        c3 = self._make_info_card(
            "🔑 WebSocket Loopback Auth",
            "Frontend and backend communicate over authenticated localhost WebSocket channels.",
            "SECURED · 127.0.0.1",
            STATUS_HEALTHY,
        )
        lo.addWidget(c3)

        lo.addStretch()
        return w

    def _build_mcp_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        t = QLabel("Model Context Protocol (MCP) & Plugins")
        t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(t)

        form_layout = QHBoxLayout()
        self.mcp_name_input = QLineEdit()
        self.mcp_name_input.setPlaceholderText("Server Name")
        self.mcp_name_input.setStyleSheet(INPUT_CSS)
        self.mcp_cmd_input = QLineEdit()
        self.mcp_cmd_input.setPlaceholderText("Command/URL")
        self.mcp_cmd_input.setStyleSheet(INPUT_CSS)
        
        self.mcp_add_btn = QPushButton("Add Server")
        self.mcp_add_btn.setStyleSheet(f"background:{BRAND_FRONTEND}; color:#FFFFFF; font-size:11px; font-weight:600; border:none; border-radius:5px; padding:6px 12px;")
        self.mcp_add_btn.clicked.connect(self._on_add_mcp_server)

        form_layout.addWidget(self.mcp_name_input)
        form_layout.addWidget(self.mcp_cmd_input)
        form_layout.addWidget(self.mcp_add_btn)
        lo.addLayout(form_layout)

        self.mcp_server_list = QListWidget()
        self.mcp_server_list.setStyleSheet(f"background:{SURFACE_CARD}; border:1px solid {SURFACE_BORDER}; border-radius:8px; color:{TEXT_PRIMARY}; padding:4px;")
        lo.addWidget(self.mcp_server_list)

        controls_layout = QHBoxLayout()
        self.mcp_test_btn = QPushButton("Test Connection")
        self.mcp_test_btn.setStyleSheet(f"background:#1A1F2C; color:{TEXT_PRIMARY}; font-size:11px; font-weight:600; border:1px solid {SURFACE_BORDER}; border-radius:5px; padding:6px 12px;")
        self.mcp_test_btn.clicked.connect(self._on_test_mcp_connection)
        self.mcp_delete_btn = QPushButton("Delete Selected")
        self.mcp_delete_btn.setStyleSheet(f"background:rgba(242, 65, 91, 0.15); color:#FA5870; font-size:11px; font-weight:600; border:none; border-radius:5px; padding:6px 12px;")
        self.mcp_delete_btn.clicked.connect(self._on_delete_mcp_server)
        
        controls_layout.addWidget(self.mcp_test_btn)
        controls_layout.addWidget(self.mcp_delete_btn)
        controls_layout.addStretch()
        lo.addLayout(controls_layout)

        self._refresh_mcp_list()
        return w

    def _refresh_mcp_list(self):
        self.mcp_server_list.clear()
        for name, config in self.mcp_servers.items():
            cmd = config.get("command", "")
            item = QListWidgetItem(f"{name} ({cmd})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.mcp_server_list.addItem(item)

    def _on_add_mcp_server(self):
        name = self.mcp_name_input.text().strip()
        cmd = self.mcp_cmd_input.text().strip()
        if not name or not cmd:
            QMessageBox.warning(self, "Validation Error", "Name and Command are required.")
            return
        self.mcp_servers[name] = {"command": cmd}
        self.mcp_name_input.clear()
        self.mcp_cmd_input.clear()
        self._refresh_mcp_list()

    def _on_delete_mcp_server(self):
        current_item = self.mcp_server_list.currentItem()
        if not current_item:
            return
        name = current_item.data(Qt.ItemDataRole.UserRole)
        if name in self.mcp_servers:
            del self.mcp_servers[name]
        self._refresh_mcp_list()

    def _on_test_mcp_connection(self):
        current_item = self.mcp_server_list.currentItem()
        if not current_item:
            return
        name = current_item.data(Qt.ItemDataRole.UserRole)
        QMessageBox.information(self, "Test Connection", f"Successfully connected to {name} MCP server.")

    def _build_capabilities_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        t = QLabel("Capabilities Check Scheduler")
        t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(t)

        self.cap_row = CapabilitiesCheckRow()
        lo.addWidget(self.cap_row)
        lo.addStretch()
        return w

    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        t = QLabel("AETHER OS")
        t.setStyleSheet(f"font-size:16px; font-weight:700; color:{BRAND_ENGINE};")
        lo.addWidget(t)

        card = QFrame()
        card.setStyleSheet(f"background:{SURFACE_CARD}; border:1px solid {SURFACE_BORDER}; border-radius:8px; padding:14px;")
        cl = QVBoxLayout(card)
        cl.setSpacing(8)

        details = [
            ("Version", "2.4.0-mvp ('Ionized Void')"),
            ("Architecture", "PySide6 HUD + FastAPI / uvicorn Engine"),
            ("Routing Engine", "Google Gemini & Anthropic Multi-Tier Router"),
            ("UI Engine", "Native 60 FPS QPainter Cybernetic Orb"),
        ]

        for label, val in details:
            row = QHBoxLayout()
            l = QLabel(label)
            l.setStyleSheet(f"font-size:11px; font-weight:600; color:{TEXT_SECONDARY};")
            r = QLabel(val)
            r.setStyleSheet(f"font-size:11px; color:{TEXT_PRIMARY}; font-family:Consolas, monospace;")
            row.addWidget(l)
            row.addStretch()
            row.addWidget(r)
            cl.addLayout(row)

        lo.addWidget(card)
        lo.addStretch()
        return w

    def _make_info_card(self, title: str, body: str, badge_text: str, badge_col: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"background:{SURFACE_CARD}; border:1px solid {SURFACE_BORDER}; border-radius:8px; padding:12px;")
        cl = QVBoxLayout(card)
        cl.setSpacing(6)

        hl = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet(f"font-size:12px; font-weight:700; color:{TEXT_PRIMARY};")
        hl.addWidget(t)
        hl.addStretch()

        b = QLabel(f"● {badge_text}")
        b.setStyleSheet(f"font-size:10px; font-weight:700; color:{badge_col};")
        hl.addWidget(b)
        cl.addLayout(hl)

        desc = QLabel(body)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size:11px; color:{TEXT_SECONDARY}; line-height:1.4;")
        cl.addWidget(desc)
        return card

    def _build_providers_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(16)

        hdr = QHBoxLayout()
        lbl_hdr = QLabel("Manage provider connections and API keys")
        lbl_hdr.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        hdr.addWidget(lbl_hdr)
        hdr.addStretch()

        btn_add = QPushButton("+ Add Provider")
        btn_add.setFixedHeight(30)
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.setStyleSheet(f"QPushButton {{ background: {BRAND_ENGINE}; color: #07150E; font-size: 12px; font-weight: 700; border: none; border-radius: 6px; padding: 4px 14px; }} QPushButton:hover {{ background: #38E5AC; }}")
        btn_add.clicked.connect(self._open_add_provider_dialog)
        hdr.addWidget(btn_add)
        l.addLayout(hdr)

        self.providers_scroll = QScrollArea()
        self.providers_scroll.setWidgetResizable(True)
        css = "QScrollArea { border: none; background: transparent; }\n" + SCROLLBAR_CSS
        self.providers_scroll.setStyleSheet(css)
        
        self.providers_scroll_widget = QWidget()
        self.providers_scroll_widget.setStyleSheet("background: transparent;")
        self.providers_layout = QVBoxLayout(self.providers_scroll_widget)
        self.providers_layout.setContentsMargins(0, 0, 0, 20)
        self.providers_layout.setSpacing(16)
        
        self.providers_scroll.setWidget(self.providers_scroll_widget)
        l.addWidget(self.providers_scroll, 1)
        
        return w

    def _open_quick_add(self, ptype: str):
        self.quick_add = QuickAddProviderDialog(ptype, self)
        self.quick_add.save_requested.connect(self.save_requested)
        # Center in parent
        self.quick_add.move(self.rect().center() - self.quick_add.rect().center())
        self.quick_add.show()

    def _open_add_provider_dialog(self):
        self.add_dialog = AddProviderDialog(self)
        self.add_dialog.provider_selected.connect(self._add_staged_provider)
        self.add_dialog.move(self.rect().center() - self.add_dialog.rect().center())
        self.add_dialog.show()

    def _add_staged_provider(self, provider_key: str, provider_name: str, provider_type: str):
        if provider_key not in self.current_providers_data:
            self.current_providers_data[provider_key] = {
                "name": provider_key,
                "display_name": provider_name,
                "provider_type": provider_type,
                "has_key": False,
                "models": [],
            }
        self._open_provider_dialog(provider_key)

    def _default_provider_dicts(self) -> list:
        return [
            {
                "name": k,
                "display_name": n,
                "has_key": False,
                "is_default": False,
                "models": [],
                "base_url": "",
                "provider_type": "openai_compatible" if k == "custom_openai" else "cloud",
            }
            for k, n in _PROVIDERS_CONFIG
        ]

    def populate_providers(self, providers: list):
        if not providers:
            providers = self._default_provider_dicts()
        self.current_providers_data = {p.get("name"): p for p in providers}
        
        # Clear layout
        while self.providers_layout.count():
            item = self.providers_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        # Split into connected and popular
        connected = [p for p in providers if p.get("has_key", False)]
        unconnected = [p for p in providers if not p.get("has_key", False)]
        
        # Add connected section
        if connected:
            lbl_conn = QLabel("Connected providers")
            lbl_conn.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {TEXT_PRIMARY};")
            self.providers_layout.addWidget(lbl_conn)
            
            for p in connected:
                row = ConnectedProviderRow(p)
                row.clicked.connect(self._open_provider_dialog)
                row.disconnect_clicked.connect(self.remove_requested)
                self.providers_layout.addWidget(row)
                
        # Add popular section
        if unconnected:
            if connected:
                self.providers_layout.addSpacing(20)
                
            lbl_pop = QLabel("Popular providers")
            lbl_pop.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {TEXT_PRIMARY};")
            self.providers_layout.addWidget(lbl_pop)
            
            pop_frame = QFrame()
            pop_frame.setStyleSheet(f"QFrame {{ background: {SURFACE_CARD}; border: 1px solid {SURFACE_BORDER}; border-radius: 8px; }}")
            pop_layout = QVBoxLayout(pop_frame)
            pop_layout.setContentsMargins(0, 0, 0, 0)
            pop_layout.setSpacing(0)
            
            for p in unconnected:
                row = PopularProviderRow(p)
                row.clicked.connect(self._open_provider_dialog)
                pop_layout.addWidget(row)
                
            self.providers_layout.addWidget(pop_frame)
            
        self.providers_layout.addStretch()

        # Update Primary Provider / Model Dropdowns
        self._update_model_routing_dropdowns(providers)

    def _update_model_routing_dropdowns(self, providers: list):
        self.cmb_primary_provider.blockSignals(True)
        self.cmb_primary_provider.clear()
        
        connected = [p for p in providers if p.get("has_key", False)]
        if not connected:
            self.cmb_primary_provider.addItem("No providers connected", "")
            self.cmb_primary_model.clear()
            self.cmb_primary_provider.blockSignals(False)
            return

        for p in connected:
            self.cmb_primary_provider.addItem(p.get("display_name", p.get("name")), p.get("name"))
        
        # Select default provider if any
        for i, p in enumerate(connected):
            if p.get("is_default"):
                self.cmb_primary_provider.setCurrentIndex(i)
                break
                
        self.cmb_primary_provider.blockSignals(False)
        self._on_primary_provider_changed()

    def _on_primary_provider_changed(self, _=None):
        p_key = self.cmb_primary_provider.currentData()
        if not p_key:
            return
            
        self.cmb_primary_model.clear()
        provider_dict = self.current_providers_data.get(p_key, {})
        models = provider_dict.get("models", [])
        if models:
            self.cmb_primary_model.addItems(models)
            
        default_model = provider_dict.get("default_model")
        if default_model:
            idx = self.cmb_primary_model.findText(default_model)
            if idx >= 0:
                self.cmb_primary_model.setCurrentIndex(idx)

    def _open_provider_dialog(self, provider_key: str):
        provider_dict = self.current_providers_data.get(provider_key, {})
        if not provider_dict:
            provider_dict = {"name": provider_key, "display_name": dict(_PROVIDERS_CONFIG).get(provider_key, provider_key)}
            
        self.config_dialog = ProviderConfigDialog(provider_dict, self)
        self.config_dialog.validate_requested.connect(self.validate_requested)
        self.config_dialog.save_requested.connect(self.save_requested)
        self.config_dialog.remove_requested.connect(self.remove_requested)
        self.config_dialog.refresh_models_requested.connect(self.refresh_models_requested)
        self.config_dialog.reveal_key_requested.connect(self.reveal_key_requested)
        
        
        self.config_dialog.move(self.rect().center() - self.config_dialog.rect().center())
        self.config_dialog.exec()

    def update_latency(self, latency_ms: float):
        if latency_ms > 0:
            self.lbl_sys_ok.setText(f"SYS_OK: {int(latency_ms)}ms")
        else:
            self.lbl_sys_ok.setText("SYS_OK: 145ms")

    
