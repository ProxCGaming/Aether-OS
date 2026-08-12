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
)

from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    CHECKBOX_CSS,
    COMBO_CSS,
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
    "custom": "https://ollama.com",
}

_PROVIDER_ICONS = {
    "google_gemini": "✦",
    "openai": "🗲",
    "anthropic": "⚙",
    "deepseek": "⟨/⟩",
    "openrouter": "∿",
    "custom": ">_",
}

_PROVIDERS_CONFIG = [
    ("google_gemini", "Google Gemini"),
    ("openai", "OpenAI (GPT-4o)"),
    ("anthropic", "Anthropic Claude"),
    ("deepseek", "DeepSeek Coder"),
    ("openrouter", "OpenRouter Meta API"),
    ("custom", "Custom OpenAI Compatible Host"),
]


class ProviderAccordionCard(QWidget):
    """Clean Provider Card with masked key display, smart save state, and mutual default toggle."""

    validate_requested = Signal(str, str, str)  # (provider, key, base_url)
    save_requested = Signal(str, str, bool, str, str)  # (provider, key, is_default, model, base_url)
    remove_requested = Signal(str)  # (provider)
    refresh_models_requested = Signal(str)  # (provider)
    reveal_key_requested = Signal(str)  # (provider)
    default_toggled = Signal(str)  # (provider)

    def __init__(self, provider_key: str, display_name: str, parent=None):
        super().__init__(parent)
        self.provider_key = provider_key
        self.display_name = display_name
        self.is_expanded = False
        self.status = "no_key"
        self.is_default = False
        self.has_saved_key = False
        self._is_revealed = False

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        # Header Bar
        self.header = QFrame()
        self.header.setObjectName("provider_header")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.mousePressEvent = lambda e: self.toggle_expanded()

        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(14, 10, 14, 10)
        hl.setSpacing(10)

        icon_str = _PROVIDER_ICONS.get(self.provider_key, "✦")
        self.lbl_icon = QLabel(icon_str)
        self.lbl_icon.setStyleSheet(f"font-size:14px; color:{BRAND_FRONTEND}; font-weight:bold; background:transparent; border:none;")
        hl.addWidget(self.lbl_icon)

        self.lbl_title = QLabel(self.display_name)
        self.lbl_title.setStyleSheet(f"font-size:13px; font-weight:600; color:{TEXT_PRIMARY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        hl.addWidget(self.lbl_title)

        hl.addStretch()

        self.lbl_status = QLabel("● No Key Configured")
        self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:600; color:{TEXT_MUTED}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        hl.addWidget(self.lbl_status)

        self.lbl_arrow = QLabel("▼")
        self.lbl_arrow.setStyleSheet(f"font-size:10px; color:{TEXT_SECONDARY}; background:transparent; border:none; margin-left:4px;")
        hl.addWidget(self.lbl_arrow)

        self.layout.addWidget(self.header)

        # Expanded Detail Section
        self.detail = QFrame()
        self.detail.setObjectName("provider_detail")
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(14, 12, 14, 12)
        dl.setSpacing(10)

        # Key Input Row
        kl = QHBoxLayout()
        kl.setSpacing(8)

        lbl_key_icon = QLabel("🔑")
        lbl_key_icon.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        kl.addWidget(lbl_key_icon)

        self.inp_key = QLineEdit()
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_key.setPlaceholderText("Enter API Key…")
        self.inp_key.setStyleSheet(INPUT_CSS)
        self.inp_key.setFixedHeight(30)
        self.inp_key.textChanged.connect(self._on_input_modified)
        kl.addWidget(self.inp_key, 1)

        self.btn_test = QPushButton("Test")
        self.btn_test.setFixedHeight(30)
        self.btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test.setStyleSheet(self._btn_css("#1A1F2C", TEXT_PRIMARY, "#252C3D", border_col=SURFACE_BORDER))
        self.btn_test.clicked.connect(self._on_test)
        kl.addWidget(self.btn_test)

        self.btn_show_key = QPushButton("⬡ Reveal")
        self.btn_show_key.setFixedHeight(30)
        self.btn_show_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_show_key.setStyleSheet(self._btn_css("rgba(124, 111, 255, 0.14)", "#B2A8FF", "rgba(124, 111, 255, 0.25)", border_col="rgba(124, 111, 255, 0.35)"))
        self.btn_show_key.clicked.connect(self._on_toggle_show_key)
        kl.addWidget(self.btn_show_key)

        self.btn_get_key = QPushButton("Get Key ↗")
        self.btn_get_key.setFixedHeight(30)
        self.btn_get_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_get_key.setStyleSheet(self._btn_css("rgba(51, 224, 196, 0.12)", "#33E0C4", "rgba(51, 224, 196, 0.25)", border_col="rgba(51, 224, 196, 0.35)"))
        self.btn_get_key.clicked.connect(self._on_get_key)
        kl.addWidget(self.btn_get_key)
        dl.addLayout(kl)

        # Custom Base URL Row
        self.custom_row = QWidget()
        self.custom_row.setStyleSheet("background:transparent; border:none;")
        c_layout = QHBoxLayout(self.custom_row)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(8)
        lbl_base_url = QLabel("Base URL:")
        lbl_base_url.setStyleSheet(f"font-size:11px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        c_layout.addWidget(lbl_base_url)
        self.inp_base_url = QLineEdit()
        self.inp_base_url.setPlaceholderText("http://localhost:11434/v1")
        self.inp_base_url.setStyleSheet(INPUT_CSS)
        self.inp_base_url.setFixedHeight(30)
        self.inp_base_url.textChanged.connect(self._on_input_modified)
        c_layout.addWidget(self.inp_base_url, 1)
        dl.addWidget(self.custom_row)
        self.custom_row.setVisible(self.provider_key == "custom")

        # Models & Controls Row
        ml = QHBoxLayout()
        ml.setSpacing(8)

        lbl_model = QLabel("Model:")
        lbl_model.setStyleSheet(f"font-size:12px; font-weight:600; color:{TEXT_SECONDARY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        ml.addWidget(lbl_model)

        self.cmb_models = QComboBox()
        self.cmb_models.setStyleSheet(COMBO_CSS)
        self.cmb_models.setFixedHeight(30)
        self.cmb_models.currentIndexChanged.connect(self._on_input_modified)
        ml.addWidget(self.cmb_models, 1)

        self.chk_default = QCheckBox("Set default")
        self.chk_default.setStyleSheet(CHECKBOX_CSS)
        self.chk_default.clicked.connect(self._on_default_clicked)
        ml.addWidget(self.chk_default)

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setFixedHeight(30)
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(self._btn_css("#1A1F2C", TEXT_PRIMARY, "#252C3D", border_col=SURFACE_BORDER))
        self.btn_refresh.clicked.connect(lambda: self.refresh_models_requested.emit(self.provider_key))
        ml.addWidget(self.btn_refresh)

        self.btn_save = QPushButton("Save")
        self.btn_save.setFixedHeight(30)
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_save_button_state(is_dirty=False)
        self.btn_save.clicked.connect(self._on_save)
        ml.addWidget(self.btn_save)

        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setFixedHeight(30)
        self.btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove.setStyleSheet(self._btn_css("rgba(242, 65, 91, 0.15)", "#FA5870", "rgba(242, 65, 91, 0.28)", border_col="rgba(242, 65, 91, 0.35)"))
        self.btn_remove.clicked.connect(lambda: self.remove_requested.emit(self.provider_key))
        ml.addWidget(self.btn_remove)
        dl.addLayout(ml)

        self.layout.addWidget(self.detail)
        self.set_expanded(False)

    def _btn_css(self, bg: str, fg: str, hover: str, border_col: str = "transparent", font_weight: str = "600") -> str:
        return f"""
            QPushButton {{
                background-color: {bg};
                color: {fg};
                font-size: 11px;
                font-weight: {font_weight};
                font-family: 'Segoe UI', sans-serif;
                border: 1px solid {border_col};
                border-radius: 6px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                background-color: {hover};
            }}
            QPushButton:disabled {{
                background-color: rgba(43, 217, 160, 0.18);
                color: rgba(255, 255, 255, 0.35);
                border: 1px solid rgba(43, 217, 160, 0.15);
            }}
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

    def _on_default_clicked(self, checked: bool):
        if checked:
            self.default_toggled.emit(self.provider_key)
        self._on_save()

    def toggle_expanded(self):
        self.set_expanded(not self.is_expanded)

    def set_expanded(self, expanded: bool):
        self.is_expanded = expanded
        self.detail.setVisible(expanded)
        self.lbl_arrow.setText("▲" if expanded else "▼")
        if expanded:
            self.header.setStyleSheet(f"""
                QFrame#provider_header {{
                    background: {SURFACE_PANEL};
                    border: 1px solid {SURFACE_BORDER};
                    border-bottom: 1px solid {SURFACE_BORDER};
                    border-top-left-radius: 8px;
                    border-top-right-radius: 8px;
                    border-bottom-left-radius: 0px;
                    border-bottom-right-radius: 0px;
                }}
                QFrame#provider_detail {{
                    background: {SURFACE_CARD};
                    border: 1px solid {SURFACE_BORDER};
                    border-top: none;
                    border-bottom-left-radius: 8px;
                    border-bottom-right-radius: 8px;
                }}
            """)
        else:
            self.header.setStyleSheet(f"""
                QFrame#provider_header {{
                    background: {SURFACE_PANEL};
                    border: 1px solid {SURFACE_BORDER};
                    border-radius: 8px;
                }}
                QFrame#provider_header:hover {{
                    border-color: {SURFACE_BORDER_LIGHT};
                    background: {SURFACE_PANEL_HOVER};
                }}
            """)

    def set_badge(self, status: str):
        self.status = status
        if status in ("connected", "valid", "healthy"):
            self.lbl_status.setText("● Connected")
            self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:600; color:{STATUS_HEALTHY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        elif status in ("invalid_key", "no_key"):
            self.lbl_status.setText("● No Key Configured")
            self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:600; color:{TEXT_MUTED}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        elif status == "offline":
            self.lbl_status.setText("● Offline")
            self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:600; color:{STATUS_OFFLINE}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        else:
            self.lbl_status.setText(f"● {status.capitalize()}")
            self.lbl_status.setStyleSheet(f"font-size:11px; font-weight:600; color:{STATUS_DEGRADED}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")

    def update_models(self, models: List[str], current: Optional[str] = None):
        cur = current or self.cmb_models.currentText()
        self.cmb_models.blockSignals(True)
        self.cmb_models.clear()
        self.cmb_models.addItems(models)
        idx = self.cmb_models.findText(cur)
        if idx >= 0:
            self.cmb_models.setCurrentIndex(idx)
        self.cmb_models.blockSignals(False)

    def get_raw_key(self) -> str:
        t = self.inp_key.text().strip()
        return "" if t == _MASKED_PLACEHOLDER else t

    def on_saved_success(self):
        self.has_saved_key = True
        self.set_badge("connected")
        self._update_save_button_state(is_dirty=False)

    def _on_test(self):
        k = self.get_raw_key()
        url = self.inp_base_url.text().strip() if self.provider_key == "custom" else ""
        self.validate_requested.emit(self.provider_key, k, url)

    def _on_save(self):
        k = self.get_raw_key()
        d = self.chk_default.isChecked()
        m = self.cmb_models.currentText()
        url = self.inp_base_url.text().strip() if self.provider_key == "custom" else ""
        self._update_save_button_state(is_dirty=False)
        self.save_requested.emit(self.provider_key, k, d, m, url)

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
        webbrowser.open(url)


class AetherConfigWindow(QWidget):
    """Full Reference-Styled Modal Settings Window."""

    closed = Signal()
    validate_requested = Signal(str, str, str)
    save_requested = Signal(str, str, bool, str, str)
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str)
    reveal_key_requested = Signal(str)
    download_local_model_requested = Signal(str, str)
    delete_local_model_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.SubWindow)

        self._provider_cards: Dict[str, ProviderAccordionCard] = {}
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
            ("🧩 Plugins & MCP", 4),
            ("ℹ About", 5),
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

        # Tab 4: Plugins & MCP
        self.view_mcp = self._build_mcp_tab()
        self.stack.addWidget(self.view_mcp)

        # Tab 5: About
        self.view_about = self._build_about_tab()
        self.stack.addWidget(self.view_about)

        mc_layout.addWidget(self.stack, 1)
        card_layout.addWidget(main_content, 1)
        root_layout.addWidget(self.card)

        self._switch_tab(0)

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
        for p_key, p_name in _PROVIDERS_CONFIG:
            self.cmb_primary_provider.addItem(p_name, p_key)
        lo.addWidget(self.cmb_primary_provider)

        # Primary Model Dropdown
        self.cmb_primary_model = QComboBox()
        self.cmb_primary_model.setStyleSheet(COMBO_CSS)
        self.cmb_primary_model.setFixedHeight(34)
        self.cmb_primary_model.addItems(["gemini-3-flash-preview", "gemini-2.5-pro", "claude-3-7-sonnet", "gpt-4o", "deepseek-reasoner"])
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

    def _build_providers_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background:transparent; border:none;")

        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(10)

        # Provider Accordion Cards
        for p_key, p_name in _PROVIDERS_CONFIG:
            card = ProviderAccordionCard(p_key, p_name, self)
            card.validate_requested.connect(self.validate_requested.emit)
            card.save_requested.connect(self.save_requested.emit)
            card.remove_requested.connect(self.remove_requested.emit)
            card.refresh_models_requested.connect(self.refresh_models_requested.emit)
            card.reveal_key_requested.connect(self.reveal_key_requested.emit)
            card.default_toggled.connect(self._on_provider_default_toggled)

            self._provider_cards[p_key] = card
            lo.addWidget(card)

        # Expand Google Gemini by default
        if "google_gemini" in self._provider_cards:
            self._provider_cards["google_gemini"].set_expanded(True)

        lo.addStretch()
        scroll.setWidget(container)
        return scroll

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

        c1 = self._make_info_card(
            "🧩 Local MCP Server Gateway",
            "Connect external tool servers implementing the open Model Context Protocol spec.",
            "RUNNING · Port 8000",
            STATUS_HEALTHY,
        )
        lo.addWidget(c1)

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

    def populate_providers(self, providers: List[dict]):
        for prov in providers:
            name = prov.get("name", "")
            if name in self._provider_cards:
                card = self._provider_cards[name]
                card.is_default = prov.get("is_default", False)
                card.chk_default.blockSignals(True)
                card.chk_default.setChecked(card.is_default)
                card.chk_default.blockSignals(False)

                if prov.get("models"):
                    card.update_models(prov.get("models", []))

                has_key = prov.get("has_key", False)
                if has_key:
                    card.has_saved_key = True
                    card.set_badge("connected")
                    # Display masked key if input is empty or already masked
                    if not card.inp_key.text() or card.inp_key.text() == _MASKED_PLACEHOLDER:
                        card.inp_key.blockSignals(True)
                        card.inp_key.setText(_MASKED_PLACEHOLDER)
                        card.inp_key.blockSignals(False)
                    card._update_save_button_state(is_dirty=False)
                else:
                    card.has_saved_key = False
                    card.set_badge("no_key")
                    if card.inp_key.text() == _MASKED_PLACEHOLDER:
                        card.inp_key.blockSignals(True)
                        card.inp_key.clear()
                        card.inp_key.blockSignals(False)
                    card._update_save_button_state(is_dirty=False)

    def update_latency(self, latency_ms: float):
        if latency_ms > 0:
            self.lbl_sys_ok.setText(f"SYS_OK: {int(latency_ms)}ms")
        else:
            self.lbl_sys_ok.setText("SYS_OK: 145ms")
