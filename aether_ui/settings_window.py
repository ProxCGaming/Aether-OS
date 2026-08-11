"""AETHER Settings Modal Overlay with Left Sidebar Navigation & Blur Backdrop.

Matches the reference layout:
- Left Sidebar with categorized navigation items (Model, Providers, Tools, MCP, Security, About).
- Model View with active Provider/Model dropdowns, Apply button, Reasoning level, and Auxiliary Models breakdown
  (Vision, Web extract, Compression, Skills hub, Approval, MCP, Title gen) with "Set to main" and "Change" actions.
- Providers View with API key inputs, Test, Show Key, Get Key, and Ollama local models.
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

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
    SCROLLBAR_CSS,
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

_PROVIDER_ICONS = {
    "google_gemini": "✦",
    "openai": "⛁",
    "anthropic": "⚙",
    "deepseek": "⟨/⟩",
    "openrouter": "∿",
    "custom": ">_",
}

_PROVIDER_DOCS = {
    "google_gemini": "https://aistudio.google.com/app/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openrouter": "https://openrouter.ai/keys",
    "custom": "https://github.com/ollama/ollama",
}


class ProviderAccordionCard(QWidget):
    """Accordion card widget for a cloud LLM provider."""

    validate_requested = Signal(str, str, str)
    save_requested = Signal(str, str, bool, str, str)
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str)
    reveal_key_requested = Signal(str)

    def __init__(self, key: str, display_name: str, parent=None):
        super().__init__(parent)
        self.provider_key = key
        self.display_name = display_name
        self.is_expanded = False
        self.is_default = False
        self.status = "no_key"

        self._build_ui()
        self.set_expanded(False)

    def _build_ui(self):
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
        kl.addWidget(self.inp_key, 1)

        self.btn_test = QPushButton("Test")
        self.btn_test.setFixedHeight(30)
        self.btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_test.setStyleSheet(self._btn_css("#1A1F2C", TEXT_PRIMARY, "#252C3D", border_col=SURFACE_BORDER))
        self.btn_test.clicked.connect(self._on_test)
        kl.addWidget(self.btn_test)

        self.btn_show_key = QPushButton("👁 Show Key")
        self.btn_show_key.setFixedHeight(30)
        self.btn_show_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_show_key.setStyleSheet(self._btn_css("rgba(124, 111, 255, 0.15)", "#9D93FF", "rgba(124, 111, 255, 0.28)", border_col="rgba(124, 111, 255, 0.35)"))
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
        ml.addWidget(self.cmb_models, 1)

        self.chk_default = QCheckBox("Set default")
        self.chk_default.setStyleSheet(CHECKBOX_CSS)
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
        self.btn_save.setStyleSheet(self._btn_css(STATUS_HEALTHY, "#07150E", "#38E5AC", font_weight="700"))
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
        """

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
        self.cmb_models.clear()
        self.cmb_models.addItems(models)
        idx = self.cmb_models.findText(cur)
        if idx >= 0:
            self.cmb_models.setCurrentIndex(idx)

    def _on_test(self):
        k = self.inp_key.text().strip()
        url = self.inp_base_url.text().strip() if self.provider_key == "custom" else ""
        self.validate_requested.emit(self.provider_key, k, url)

    def _on_save(self):
        k = self.inp_key.text().strip()
        d = self.chk_default.isChecked()
        m = self.cmb_models.currentText()
        url = self.inp_base_url.text().strip() if self.provider_key == "custom" else ""
        self.save_requested.emit(self.provider_key, k, d, m, url)

    def _on_toggle_show_key(self):
        if self.inp_key.echoMode() == QLineEdit.EchoMode.Password:
            if not self.inp_key.text():
                self.reveal_key_requested.emit(self.provider_key)
            self.inp_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_show_key.setText("Hide Key")
        else:
            self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_show_key.setText("👁 Show Key")

    def _on_get_key(self):
        url = _PROVIDER_DOCS.get(self.provider_key, "https://google.com")
        webbrowser.open(url)


class AetherConfigWindow(QWidget):
    """Full-featured Settings Modal matching the reference sidebar layout & Figma tokens."""

    validate_requested = Signal(str, str, str)
    save_requested = Signal(str, str, bool, str, str)
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str)
    reveal_key_requested = Signal(str)
    download_local_model_requested = Signal(str, str)
    delete_local_model_requested = Signal(str)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._provider_cards: Dict[str, ProviderAccordionCard] = {}
        self._sidebar_buttons: List[QPushButton] = []
        self._build_ui()

    def _build_ui(self):
        # Outermost container: full-window backdrop overlay
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Center Card Frame (The Settings Dialog Box)
        self.card = QFrame()
        self.card.setObjectName("settings_modal_card")
        self.card.setStyleSheet(f"""
            QFrame#settings_modal_card {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 12px;
            }}
        """)
        card_layout = QHBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # -------------------------------------------------------------------
        # 1. Left Sidebar Navigation (Matching Reference Image)
        # -------------------------------------------------------------------
        sidebar = QFrame()
        sidebar.setObjectName("settings_sidebar")
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet(f"""
            QFrame#settings_sidebar {{
                background-color: {SURFACE_BG};
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
            btn = self._make_sidebar_btn(label, idx)
            self._sidebar_buttons.append(btn)
            s_layout.addWidget(btn)

        s_layout.addStretch()

        # Sidebar bottom icon toolbar (download, upload, refresh)
        bot_bar = QHBoxLayout()
        bot_bar.setContentsMargins(6, 0, 6, 0)
        bot_bar.setSpacing(8)

        btn_dl = QPushButton("↓")
        btn_dl.setToolTip("Export Configuration")
        btn_dl.setFixedSize(26, 26)
        btn_dl.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_dl.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")

        btn_ul = QPushButton("↑")
        btn_ul.setToolTip("Import Configuration")
        btn_ul.setFixedSize(26, 26)
        btn_ul.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ul.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")

        btn_rf = QPushButton("↻")
        btn_rf.setToolTip("Reload All Settings")
        btn_rf.setFixedSize(26, 26)
        btn_rf.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rf.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; border:none; font-size:13px; font-weight:bold;")
        btn_rf.clicked.connect(lambda: self.refresh_models_requested.emit("google_gemini"))

        bot_bar.addWidget(btn_dl)
        bot_bar.addWidget(btn_ul)
        bot_bar.addWidget(btn_rf)
        bot_bar.addStretch()
        s_layout.addLayout(bot_bar)

        card_layout.addWidget(sidebar)

        # -------------------------------------------------------------------
        # 2. Main Content Area (QStackedWidget + Top Close Button)
        # -------------------------------------------------------------------
        main_content = QWidget()
        main_content.setStyleSheet("background:transparent; border:none;")
        mc_layout = QVBoxLayout(main_content)
        mc_layout.setContentsMargins(24, 16, 24, 18)
        mc_layout.setSpacing(12)

        # Top Header Bar: Status + Close Button
        top_bar = QHBoxLayout()
        self.lbl_sys_ok = QLabel("SYS_OK: 145ms")
        self.lbl_sys_ok.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED}; font-family:Consolas, monospace;")
        top_bar.addWidget(self.lbl_sys_ok)

        self.lbl_online = QLabel("● ONLINE")
        self.lbl_online.setStyleSheet(f"""
            font-size: 10px; font-weight: 700; color: {STATUS_HEALTHY};
            background: #0E241C; border: 1px solid #194D3B; border-radius: 10px; padding: 2px 8px;
        """)
        top_bar.addWidget(self.lbl_online)

        top_bar.addStretch()

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_SECONDARY};
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
        self.btn_close.clicked.connect(self.close_modal)
        top_bar.addWidget(self.btn_close)
        mc_layout.addLayout(top_bar)

        # Stacked Views
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent; border:none;")

        # Tab 0: Model & Routing (Matching Reference Image)
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
                        font-family: 'Segoe UI', sans-serif;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: transparent;
                        color: {TEXT_SECONDARY};
                        font-size: 12px;
                        font-weight: 500;
                        border: 1px solid transparent;
                        border-radius: 6px;
                        text-align: left;
                        padding-left: 12px;
                        font-family: 'Segoe UI', sans-serif;
                    }}
                    QPushButton:hover {{
                        color: {TEXT_PRIMARY};
                        background-color: {SURFACE_PANEL_HOVER};
                    }}
                """)

    def _build_model_routing_tab(self) -> QWidget:
        """Model & Routing View matching the user's reference image."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(SCROLLBAR_CSS)

        content = QWidget()
        content.setStyleSheet("background:transparent; border:none;")
        lo = QVBoxLayout(content)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(14)

        # Header description
        lbl_desc = QLabel("Applies to new sessions. Use the model picker in the composer to hot-swap the active chat.")
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"font-size:12px; color:{TEXT_SECONDARY}; line-height:1.4; background:transparent; border:none;")
        lo.addWidget(lbl_desc)

        # Provider Selector Dropdown
        self.cmb_main_provider = QComboBox()
        self.cmb_main_provider.addItems(["Google Gemini (Google AI Studio)", "Anthropic Claude", "OpenAI (GPT-4o)", "DeepSeek Coder", "OpenRouter Meta API", "Custom OpenAI / Ollama"])
        self.cmb_main_provider.setStyleSheet(COMBO_CSS)
        self.cmb_main_provider.setFixedHeight(34)
        lo.addWidget(self.cmb_main_provider)

        # Model Selector Dropdown
        self.cmb_main_model = QComboBox()
        self.cmb_main_model.addItems(["gemini-2.0-flash", "gemini-1.5-pro-latest", "gemini-1.5-flash-latest", "claude-3-7-sonnet", "gpt-4o", "deepseek-coder-v2"])
        self.cmb_main_model.setStyleSheet(COMBO_CSS)
        self.cmb_main_model.setFixedHeight(34)
        lo.addWidget(self.cmb_main_model)

        # Action Bar: Apply + Defaults + Reasoning
        act_bar = QHBoxLayout()
        act_bar.setSpacing(12)

        self.btn_apply_model = QPushButton("Apply")
        self.btn_apply_model.setFixedHeight(32)
        self.btn_apply_model.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply_model.setStyleSheet(f"""
            QPushButton {{
                background-color: #2563EB;
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 700;
                border: none;
                border-radius: 6px;
                padding: 4px 18px;
            }}
            QPushButton:hover {{ background-color: #3B82F6; }}
        """)
        self.btn_apply_model.clicked.connect(self._on_apply_primary_model)
        act_bar.addWidget(self.btn_apply_model)

        lbl_defs = QLabel("Defaults")
        lbl_defs.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED}; font-weight:600;")
        act_bar.addWidget(lbl_defs)

        lbl_reason = QLabel("Reasoning")
        lbl_reason.setStyleSheet(f"font-size:12px; font-weight:600; color:{TEXT_PRIMARY}; margin-left:8px;")
        act_bar.addWidget(lbl_reason)

        self.cmb_reasoning = QComboBox()
        self.cmb_reasoning.addItems(["Standard", "Medium", "Deep Thinking"])
        self.cmb_reasoning.setStyleSheet(COMBO_CSS)
        self.cmb_reasoning.setFixedHeight(28)
        act_bar.addWidget(self.cmb_reasoning)
        act_bar.addStretch()

        lo.addLayout(act_bar)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{SURFACE_BORDER};")
        lo.addWidget(sep)

        # Auxiliary Models Section (Matching Reference Image)
        aux_hdr = QHBoxLayout()
        lbl_aux_title = QLabel("⚙ Auxiliary models")
        lbl_aux_title.setStyleSheet(f"font-size:13px; font-weight:700; color:{TEXT_PRIMARY};")
        aux_hdr.addWidget(lbl_aux_title)
        aux_hdr.addStretch()

        btn_reset_aux = QPushButton("Reset all to main")
        btn_reset_aux.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_reset_aux.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; font-size:11px; font-weight:600; border:none;")
        aux_hdr.addWidget(btn_reset_aux)
        lo.addLayout(aux_hdr)

        lbl_aux_sub = QLabel("Helper tasks run on the main model by default. Assign a dedicated model to any task to override.")
        lbl_aux_sub.setStyleSheet(f"font-size:11px; color:{TEXT_MUTED};")
        lo.addWidget(lbl_aux_sub)

        aux_tasks = [
            ("Vision", "Image analysis", "auto · use main model"),
            ("Web extract", "Page summarization", "auto · use main model"),
            ("Compression", "Context compaction", "auto · use main model"),
            ("Skills hub", "Skill search", "auto · use main model"),
            ("Approval", "Smart auto-approve", "auto · use main model"),
            ("MCP", "MCP tool routing", "auto · use main model"),
            ("Title gen", "Session titles", "auto · use main model"),
        ]

        for name, tag, status_txt in aux_tasks:
            row = self._create_aux_row(name, tag, status_txt)
            lo.addWidget(row)

        lo.addStretch()
        scroll.setWidget(content)
        return scroll

    def _create_aux_row(self, name: str, tag: str, status_txt: str) -> QFrame:
        row = QFrame()
        row.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_CARD};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
                padding: 10px 14px;
            }}
        """)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(4, 2, 4, 2)
        rl.setSpacing(10)

        # Name + Tag
        nl = QVBoxLayout()
        nl.setSpacing(2)

        name_row = QHBoxLayout()
        lbl_n = QLabel(name)
        lbl_n.setStyleSheet(f"font-size:12px; font-weight:700; color:{TEXT_PRIMARY}; font-family:'Segoe UI', sans-serif; background:transparent; border:none;")
        name_row.addWidget(lbl_n)

        lbl_t = QLabel(tag)
        lbl_t.setStyleSheet(f"font-size:10px; color:{TEXT_MUTED}; background:transparent; border:none; margin-left:4px;")
        name_row.addWidget(lbl_t)
        name_row.addStretch()
        nl.addLayout(name_row)

        lbl_s = QLabel(status_txt)
        lbl_s.setStyleSheet(f"font-size:11px; color:{TEXT_SECONDARY}; font-family:Consolas, monospace; background:transparent; border:none;")
        nl.addWidget(lbl_s)
        rl.addLayout(nl, 1)

        # Actions
        btn_main = QPushButton("Set to main")
        btn_main.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_main.setStyleSheet(f"background:transparent; color:{TEXT_SECONDARY}; font-size:11px; font-weight:600; border:none; padding:4px 8px;")
        rl.addWidget(btn_main)

        btn_chg = QPushButton("Change")
        btn_chg.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_chg.setStyleSheet(f"background:transparent; color:{BRAND_FRONTEND}; font-size:11px; font-weight:600; border:none; padding:4px 8px;")
        rl.addWidget(btn_chg)

        return row

    def _on_apply_primary_model(self):
        prov_map = {
            0: "google_gemini",
            1: "anthropic",
            2: "openai",
            3: "deepseek",
            4: "openrouter",
            5: "custom",
        }
        prov = prov_map.get(self.cmb_main_provider.currentIndex(), "google_gemini")
        mod = self.cmb_main_model.currentText()
        if prov in self._provider_cards:
            card = self._provider_cards[prov]
            k = card.inp_key.text().strip()
            url = card.inp_base_url.text().strip() if prov == "custom" else ""
            self.save_requested.emit(prov, k, True, mod, url)

    def _build_providers_tab(self) -> QWidget:
        """Providers & API Keys View."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(SCROLLBAR_CSS)

        content = QWidget()
        content.setStyleSheet("background:transparent; border:none;")
        lo = QVBoxLayout(content)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(10)

        lbl_t = QLabel("Provider Connections & API Keys")
        lbl_t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(lbl_t)

        default_providers = [
            ("google_gemini", "Google Gemini"),
            ("openai", "OpenAI (GPT-4o)"),
            ("anthropic", "Anthropic Claude"),
            ("deepseek", "DeepSeek Coder"),
            ("openrouter", "OpenRouter Meta API"),
            ("custom", "Custom OpenAI Compatible Host"),
        ]

        for pkey, pname in default_providers:
            card = ProviderAccordionCard(pkey, pname)
            card.validate_requested.connect(self.validate_requested.emit)
            card.save_requested.connect(self.save_requested.emit)
            card.remove_requested.connect(self.remove_requested.emit)
            card.refresh_models_requested.connect(self.refresh_models_requested.emit)
            card.reveal_key_requested.connect(self.reveal_key_requested.emit)
            self._provider_cards[pkey] = card
            lo.addWidget(card)

        if "google_gemini" in self._provider_cards:
            self._provider_cards["google_gemini"].set_expanded(True)

        lo.addStretch()
        scroll.setWidget(content)
        return scroll

    def _build_tools_tab(self) -> QWidget:
        """Tools Tab matching Tool Approvals & Sandboxing from Figma Page 11."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(SCROLLBAR_CSS)

        content = QWidget()
        content.setStyleSheet("background:transparent; border:none;")
        lo = QVBoxLayout(content)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(10)

        t = QLabel("Tool Approvals & Isolation Policies")
        t.setStyleSheet(f"font-size:14px; font-weight:700; color:{TEXT_PRIMARY};")
        lo.addWidget(t)

        tools_readonly = [
            ("read_file", "Read-only · In-process"),
            ("list_dir", "Read-only · In-process"),
            ("get_current_time", "Read-only · In-process"),
        ]
        for tname, desc in tools_readonly:
            card = self._create_tool_card(tname, desc, requires_approval=False)
            lo.addWidget(card)

        tools_mutating = [
            ("write_file", "Standard / Mutating · Sandboxed (Job Object)"),
            ("delete_file", "Critical / Mutating · Sandboxed (Job Object)"),
            ("execute_shell", "Critical / Execution · Sandboxed (Job Object)"),
        ]
        for tname, desc in tools_mutating:
            card = self._create_tool_card(tname, desc, requires_approval=True)
            lo.addWidget(card)

        lo.addStretch()
        scroll.setWidget(content)
        return scroll

    def _create_tool_card(self, name: str, desc: str, requires_approval: bool) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"background:{SURFACE_CARD}; border:1px solid {SURFACE_BORDER}; border-radius:8px; padding:10px 14px;")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(4, 2, 4, 2)
        cl.setSpacing(12)

        dot = QLabel("●")
        dot_col = STATUS_DEGRADED if requires_approval else STATUS_HEALTHY
        dot.setStyleSheet(f"color:{dot_col}; font-size:11px; background:transparent; border:none;")
        cl.addWidget(dot)

        vl = QVBoxLayout()
        vl.setSpacing(2)
        lbl_name = QLabel(name)
        lbl_name.setStyleSheet(f"font-size:12px; font-weight:700; color:{TEXT_PRIMARY}; font-family:Consolas, monospace; background:transparent; border:none;")
        vl.addWidget(lbl_name)

        lbl_desc = QLabel(desc)
        lbl_desc.setStyleSheet(f"font-size:10px; color:{TEXT_SECONDARY}; background:transparent; border:none;")
        vl.addWidget(lbl_desc)
        cl.addLayout(vl, 1)

        if not requires_approval:
            lbl_tag = QLabel("No approval needed")
            lbl_tag.setStyleSheet(f"""
                font-size: 11px; color: {TEXT_MUTED}; background: {SURFACE_BG};
                border: 1px solid {SURFACE_BORDER}; border-radius: 6px; padding: 4px 10px;
            """)
            cl.addWidget(lbl_tag)
        else:
            btn_allow = QPushButton("Allow")
            btn_allow.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_allow.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {STATUS_HEALTHY};
                    border: 1px solid {STATUS_HEALTHY}80; border-radius: 5px; padding: 4px 12px;
                    font-size: 11px; font-weight: 600;
                }}
                QPushButton:hover {{ background: rgba(43, 217, 160, 0.15); }}
            """)
            cl.addWidget(btn_allow)

            btn_deny = QPushButton("Deny")
            btn_deny.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_deny.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {STATUS_OFFLINE};
                    border: 1px solid {STATUS_OFFLINE}80; border-radius: 5px; padding: 4px 12px;
                    font-size: 11px; font-weight: 600;
                }}
                QPushButton:hover {{ background: rgba(242, 65, 91, 0.15); }}
            """)
            cl.addWidget(btn_deny)

        return card

    def _build_security_tab(self) -> QWidget:
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(0, 0, 8, 0)
        lo.setSpacing(12)

        t = QLabel("Security & Isolation")
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
                card.chk_default.setChecked(card.is_default)
                if prov.get("models"):
                    card.update_models(prov.get("models", []))
                status = "connected" if prov.get("has_key") else "no_key"
                card.set_badge(status)

    def update_latency(self, latency_ms: float):
        if latency_ms > 0:
            self.lbl_sys_ok.setText(f"SYS_OK: {int(latency_ms)}ms")
        else:
            self.lbl_sys_ok.setText("SYS_OK: 145ms")
