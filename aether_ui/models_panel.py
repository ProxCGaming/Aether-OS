"""Wide models panel (~440-560px) with Cloud, Local (Ollama) tabs, Capabilities Check scheduler row, proportional edge resizing, and full dark theme."""
import datetime
import functools
from pathlib import Path
from typing import Optional
import webbrowser

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_DEFAULT_PANEL_W = 440
_MIN_PANEL_W = 320
_MAX_PANEL_W = 560

# ---------------------------------------------------------------------------
# Stylesheet constants
# ---------------------------------------------------------------------------
_SEG_ACTIVE = """QPushButton {
    background: #1E293B; color: #E2E8F0; font-size: 12px; font-weight: 700;
    border: 1px solid #334155; border-radius: 6px; padding: 6px 20px;
}"""

_SEG_INACTIVE = """QPushButton {
    background: transparent; color: #64748B; font-size: 12px; font-weight: 600;
    border: 1px solid transparent; border-radius: 6px; padding: 6px 20px;
} QPushButton:hover { color: #94A3B8; }"""

_LINK_BTN = """QPushButton {
    background: transparent; color: #64748B; border: none; font-size: 14px; padding: 4px;
} QPushButton:hover { color: #38BDF8; }"""

_KEY_INPUT = """QLineEdit {
    background: #161B22; color: #E6EDF3; font-size: 12px;
    border: 1px solid #30363D; border-radius: 6px; padding: 6px 10px;
} QLineEdit:focus { border: 1px solid #58A6FF; }"""

_SMALL_BTN = """QPushButton {{
    background: {bg}; color: {fg}; font-size: 11px; font-weight: 600;
    border: none; border-radius: 5px; padding: 5px 12px;
}} QPushButton:hover {{ background: {hover}; }}
QPushButton:disabled {{ background: #1E293B; color: #475569; }}"""

_COMBO_CSS = """QComboBox {
    background: #161B22; color: #58A6FF; font-size: 11px; font-weight: 600;
    border: 1px solid #30363D; border-radius: 6px; padding: 5px 8px; min-width: 150px;
} QComboBox:hover { border-color: #58A6FF; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid #8B949E; margin-right: 6px; }
QComboBox QAbstractItemView { background: #161B22; color: #E6EDF3;
    selection-background-color: #1F6FEB; selection-color: #FFFFFF;
    border: 1px solid #30363D; border-radius: 6px; padding: 4px; outline: none; }"""

_SCROLL_CSS = """
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: #0D1219; width: 8px; margin: 0px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #1E293B; min-height: 20px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #334155; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


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

        # Info layout
        info_lo = QVBoxLayout()
        info_lo.setSpacing(2)
        lbl_name = QLabel(name)
        lbl_name.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6EDF3; border: none;")
        info_lo.addWidget(lbl_name)

        lbl_sub = QLabel(path if downloaded and path else size)
        lbl_sub.setStyleSheet("font-size: 11px; color: #8B949E; border: none;")
        info_lo.addWidget(lbl_sub)
        layout.addLayout(info_lo, 1)

        # Progress bar (hidden by default)
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

        # Action button
        if downloaded:
            self.btn_action = QPushButton("Delete")
            self.btn_action.setStyleSheet(_SMALL_BTN.format(bg="#382020", fg="#F85149", hover="#4D2828"))
            self.btn_action.clicked.connect(lambda: self.delete_clicked.emit(self.name))
        else:
            self.btn_action = QPushButton("Download")
            self.btn_action.setStyleSheet(_SMALL_BTN.format(bg="#1F6FEB", fg="#FFFFFF", hover="#388BFD"))
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
            self.btn_action.setStyleSheet(_SMALL_BTN.format(bg="#382020", fg="#F85149", hover="#4D2828"))


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

        # Header card
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

        # Body panel
        self.body = QFrame()
        self.body.setVisible(False)
        self.body.setStyleSheet("QFrame { background: #0D1117; border: 1px solid #30363D; border-top: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; padding: 10px; }")
        body_lo = QVBoxLayout(self.body)
        body_lo.setSpacing(10)

        # Method selection (Option A / Option C / Both)
        lbl_method = QLabel("Method Choice:")
        lbl_method.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E;")
        body_lo.addWidget(lbl_method)

        method_lo = QHBoxLayout()
        self.rb_opt_a = QRadioButton("Option A")
        self.rb_opt_a.setToolTip("Option A: Zero-cost static benchmark evaluation using curated data (MMLU, HumanEval, MATH).")
        self.rb_opt_c = QRadioButton("Option C")
        self.rb_opt_c.setToolTip("Option C: Live lightweight test suite executed directly against reachable models.")
        self.rb_both = QRadioButton("Both (Recommended)")
        self.rb_both.setToolTip("Both: Merges static benchmark data with live empirical self-tests for high accuracy.")
        self.rb_both.setChecked(True)

        for rb in (self.rb_opt_a, self.rb_opt_c, self.rb_both):
            rb.setStyleSheet("QRadioButton { color: #C9D1D9; font-size: 11px; }")
            method_lo.addWidget(rb)

        body_lo.addLayout(method_lo)

        # Explanatory description card
        self.lbl_method_desc = QLabel(
            "• Option A: Zero-cost curated benchmark cards (MMLU, HumanEval, MATH).\n"
            "• Option C: Live micro-evals measuring accuracy and latency on reachable models.\n"
            "• Both: Synthesizes both sources for verified capability routing."
        )
        self.lbl_method_desc.setStyleSheet("font-size: 10px; color: #8B949E; line-height: 1.4; background: #161B22; border: 1px solid #21262D; border-radius: 6px; padding: 6px;")
        body_lo.addWidget(self.lbl_method_desc)

        # Interval selection
        lbl_interval = QLabel("Schedule Interval:")
        lbl_interval.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E;")
        body_lo.addWidget(lbl_interval)

        int_lo = QHBoxLayout()
        self.cmb_interval = QComboBox()
        self.cmb_interval.setStyleSheet(_COMBO_CSS)
        self.cmb_interval.addItems(["weekly", "daily", "monthly", "custom", "none"])
        self.cmb_interval.currentTextChanged.connect(self._on_schedule_changed)
        int_lo.addWidget(self.cmb_interval)

        self.btn_run_now = QPushButton("Run Now")
        self.btn_run_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run_now.setStyleSheet(_SMALL_BTN.format(bg="#238636", fg="#FFFFFF", hover="#2EA043"))
        self.btn_run_now.clicked.connect(self._on_run_now)
        int_lo.addWidget(self.btn_run_now)
        body_lo.addLayout(int_lo)

        # Next/Last run display
        self.lbl_next_run = QLabel("Next run: Scheduled • Last run: Never")
        self.lbl_next_run.setStyleSheet("font-size: 11px; color: #7EE787;")
        body_lo.addWidget(self.lbl_next_run)

        # Run history list
        lbl_hist = QLabel("Run History:")
        lbl_hist.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E; margin-top: 4px;")
        body_lo.addWidget(lbl_hist)

        # Dedicated scroll area for run history
        self.hist_scroll = QScrollArea()
        self.hist_scroll.setWidgetResizable(True)
        self.hist_scroll.setFixedHeight(120)
        self.hist_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.hist_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hist_scroll.setStyleSheet("""
            QScrollArea { background: #161B22; border: 1px solid #30363D; border-radius: 6px; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { background: #0D1117; width: 6px; margin: 0px; border-radius: 3px; }
            QScrollBar::handle:vertical { background: #30363D; min-height: 16px; border-radius: 3px; }
            QScrollBar::handle:vertical:hover { background: #484F58; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self.hist_scroll.verticalScrollBar().setSingleStep(16)

        self.hist_content = QWidget()
        self.hist_content.setStyleSheet("background: transparent;")
        self.history_layout = QVBoxLayout(self.hist_content)
        self.history_layout.setContentsMargins(6, 6, 6, 6)
        self.history_layout.setSpacing(4)
        self.hist_scroll.setWidget(self.hist_content)
        body_lo.addWidget(self.hist_scroll)

        main_lo.addWidget(self.body)

    def toggle_expand(self):
        self.is_expanded = not self.is_expanded
        self.body.setVisible(self.is_expanded)
        self.lbl_arrow.setText("▲" if self.is_expanded else "▼")

    def _selected_method(self) -> str:
        if self.rb_opt_a.isChecked(): return "Option A"
        if self.rb_opt_c.isChecked(): return "Option C"
        return "Both"

    def _on_schedule_changed(self, interval: str):
        method = self._selected_method()
        self.lbl_status.setText(f"{interval.capitalize()} • {method}")
        self.set_schedule_requested.emit(interval, method)

    def _on_run_now(self):
        method = self._selected_method()
        self.run_now_requested.emit(method)

    def update_history(self, history: list, summary: Optional[dict] = None):
        while self.history_layout.count() > 0:
            item = self.history_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if summary:
            sched = summary.get("schedule", "weekly").capitalize()
            meth = summary.get("method", "Both")
            self.lbl_status.setText(f"{sched} • {meth}")

            next_ts = summary.get("next_run_ts")
            next_str = datetime.datetime.fromtimestamp(next_ts).strftime("%m-%d %H:%M") if next_ts else "Scheduled"
            last_run = summary.get("last_run")
            last_str = datetime.datetime.fromtimestamp(last_run["started_at"]).strftime("%m-%d %H:%M") if last_run else "Never"
            self.lbl_next_run.setText(f"Next run: {next_str} • Last run: {last_str}")

        if not history:
            empty = QLabel("No capability check runs recorded yet.")
            empty.setStyleSheet("font-size: 11px; color: #484F58; padding: 4px;")
            self.history_layout.addWidget(empty)
            return

        for run in history:
            card = QFrame()
            card.setStyleSheet("""
                QFrame {
                    background: #0D1117;
                    border: 1px solid #21262D;
                    border-radius: 4px;
                    padding: 3px 6px;
                }
            """)
            card_lo = QHBoxLayout(card)
            card_lo.setContentsMargins(4, 3, 4, 3)
            card_lo.setSpacing(6)

            ts = run.get("started_at", 0)
            ts_str = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "Recent"
            lbl_ts = QLabel(ts_str)
            lbl_ts.setStyleSheet("font-size: 10px; color: #8B949E; font-family: Consolas, monospace; border: none;")
            card_lo.addWidget(lbl_ts)

            meth = run.get("method", "Both")
            lbl_meth = QLabel(meth)
            lbl_meth.setStyleSheet("font-size: 10px; font-weight: 600; color: #38BDF8; background: #161B22; border-radius: 3px; padding: 1px 4px; border: none;")
            card_lo.addWidget(lbl_meth)

            status = str(run.get("status", "completed")).lower()
            if "success" in status or "completed" in status:
                s_col, s_txt = "#3FB950", "✔ Done"
            elif "fail" in status or "error" in status:
                s_col, s_txt = "#F85149", "✖ Error"
            else:
                s_col, s_txt = "#E3B341", status.capitalize()

            lbl_stat = QLabel(s_txt)
            lbl_stat.setStyleSheet(f"font-size: 10px; font-weight: 600; color: {s_col}; border: none;")
            card_lo.addWidget(lbl_stat)

            card_lo.addStretch()

            tested = run.get("models_tested", 0)
            lbl_tested = QLabel(f"{tested} models")
            lbl_tested.setStyleSheet("font-size: 10px; color: #6E7681; border: none;")
            card_lo.addWidget(lbl_tested)

            self.history_layout.addWidget(card)

        self.history_layout.addStretch()


class ProviderRow(QWidget):
    """Accordion expandable row for Cloud providers."""
    toggled = Signal(str, bool)
    refresh_requested = Signal(str)
    reveal_key_requested = Signal(str)

    def __init__(
        self,
        name: str,
        display_name: str,
        icon: str,
        key_url: str,
        models: list,
        supports_custom_url: bool = False,
        base_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.name = name
        self.display_name = display_name
        self.icon = icon
        self.key_url = key_url
        self.models = models
        self.supports_custom_url = supports_custom_url
        self.base_url = base_url
        self.is_expanded = False
        self._build_ui()

    def _build_ui(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 4)
        lo.setSpacing(0)

        # Header card
        self.hdr = QFrame()
        self.hdr.setFixedHeight(44)
        self.hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hdr.setStyleSheet("""
            QFrame { background: #161B22; border: 1px solid #30363D; border-radius: 8px; }
            QFrame:hover { border-color: #58A6FF; }
        """)
        hdr_lo = QHBoxLayout(self.hdr)
        hdr_lo.setContentsMargins(12, 0, 12, 0)

        lbl_icon = QLabel(self.icon)
        lbl_icon.setStyleSheet("font-size: 14px; border: none;")
        hdr_lo.addWidget(lbl_icon)

        lbl_name = QLabel(self.display_name)
        lbl_name.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6EDF3; border: none;")
        hdr_lo.addWidget(lbl_name)

        hdr_lo.addStretch()

        self.lbl_badge = QLabel("No Key")
        self.lbl_badge.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E; border: none;")
        hdr_lo.addWidget(self.lbl_badge)

        self.lbl_arrow = QLabel("▼")
        self.lbl_arrow.setStyleSheet("font-size: 10px; color: #8B949E; border: none;")
        hdr_lo.addWidget(self.lbl_arrow)

        self.hdr.mousePressEvent = lambda e: self.toggle_expand()
        lo.addWidget(self.hdr)

        from aether_ui.models_panel import ProviderDetailPanel
        self.detail = ProviderDetailPanel(
            self.name,
            self.key_url,
            self.models,
            supports_custom_url=self.supports_custom_url,
            base_url=self.base_url,
        )
        self.detail.refresh_requested.connect(self.refresh_requested.emit)
        self.detail.reveal_key_requested.connect(lambda prov: self.reveal_key_requested.emit(prov))
        self.detail.setVisible(False)
        lo.addWidget(self.detail)

    def toggle_expand(self):
        self.is_expanded = not self.is_expanded
        self.detail.setVisible(self.is_expanded)
        self.lbl_arrow.setText("▲" if self.is_expanded else "▼")
        self.toggled.emit(self.name, self.is_expanded)

    def set_badge(self, status: str):
        if status == "connected":
            self.lbl_badge.setText("● Connected")
            self.lbl_badge.setStyleSheet("font-size: 11px; font-weight: 600; color: #3FB950; border: none;")
        elif status == "invalid_key":
            self.lbl_badge.setText("● Invalid Key")
            self.lbl_badge.setStyleSheet("font-size: 11px; font-weight: 600; color: #F85149; border: none;")
        else:
            self.lbl_badge.setText("No Key")
            self.lbl_badge.setStyleSheet("font-size: 11px; font-weight: 600; color: #8B949E; border: none;")

    def update_models(self, models: list):
        self.models = list(models)
        self.detail.update_models(models)


class ProviderDetailPanel(QFrame):
    """Expandable inner body showing masked API key input, optional Base URL, and actions."""
    validate_requested = Signal(str, str, str)           # provider, api_key, base_url
    save_requested = Signal(str, str, bool, str, str)    # provider, api_key, is_default, default_model, base_url
    remove_requested = Signal(str)
    refresh_requested = Signal(str)
    reveal_key_requested = Signal(str)                   # provider

    def __init__(
        self,
        provider: str,
        key_url: str,
        models: list,
        supports_custom_url: bool = False,
        base_url: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.provider = provider
        self.key_url = key_url
        self.models = models
        self.supports_custom_url = supports_custom_url or provider.startswith("custom")
        self.setStyleSheet("QFrame { background: #0D1117; border: 1px solid #30363D; border-top: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; padding: 8px; }")

        lo = QVBoxLayout(self)
        lo.setSpacing(8)

        # Base URL input row (if provider supports custom base URL)
        if self.supports_custom_url:
            ulo = QHBoxLayout()
            lbl_u = QLabel("Base URL:")
            lbl_u.setStyleSheet("font-size: 11px; color: #8B949E;")
            ulo.addWidget(lbl_u)

            self.inp_url = QLineEdit()
            self.inp_url.setPlaceholderText("https://api.openai.com/v1 or http://localhost:8000/v1")
            self.inp_url.setText(base_url)
            self.inp_url.setStyleSheet(_KEY_INPUT)
            ulo.addWidget(self.inp_url, 1)
            lo.addLayout(ulo)
        else:
            self.inp_url = None

        # Key input row
        klo = QHBoxLayout()
        self.inp_key = QLineEdit()
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_key.setPlaceholderText("Enter API key…")
        self.inp_key.setStyleSheet(_KEY_INPUT)
        klo.addWidget(self.inp_key, 1)

        self.btn_validate = QPushButton("Test")
        self.btn_validate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_validate.setStyleSheet(_SMALL_BTN.format(bg="#1E293B", fg="#E2E8F0", hover="#334155"))
        self.btn_validate.clicked.connect(self._on_validate)
        klo.addWidget(self.btn_validate)

        self.btn_show_key = QPushButton("Show Key")
        self.btn_show_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_show_key.setToolTip("Decrypt and reveal the stored API key (auto-hides after 30s)")
        self.btn_show_key.setStyleSheet(_SMALL_BTN.format(bg="#7C3AED", fg="#FFFFFF", hover="#8B5CF6"))
        self.btn_show_key.clicked.connect(self._on_show_key)
        klo.addWidget(self.btn_show_key)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._hide_key)

        self.btn_get_key = QPushButton("Get Key ↗")
        self.btn_get_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_get_key.setStyleSheet(_SMALL_BTN.format(bg="#1F6FEB", fg="#FFFFFF", hover="#388BFD"))
        if self.key_url:
            self.btn_get_key.clicked.connect(lambda: webbrowser.open(self.key_url))
        else:
            self.btn_get_key.setVisible(False)
        klo.addWidget(self.btn_get_key)
        lo.addLayout(klo)

        mlo = QHBoxLayout()
        lbl_mod = QLabel("Model:")
        lbl_mod.setStyleSheet("font-size: 11px; color: #8B949E;")
        mlo.addWidget(lbl_mod)

        self.cmb_model = QComboBox()
        self.cmb_model.setStyleSheet(_COMBO_CSS)
        self.cmb_model.addItems(self.models)
        if self.supports_custom_url:
            self.cmb_model.setEditable(True)
        mlo.addWidget(self.cmb_model)

        self.chk_default = QCheckBox("Set Default")
        self.chk_default.setStyleSheet("QCheckBox { color: #94A3B8; font-size: 11px; }")
        mlo.addWidget(self.chk_default)

        mlo.addStretch()

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setToolTip("Re-query models & probe reachability directly from provider endpoint")
        self.btn_refresh.setStyleSheet(_SMALL_BTN.format(bg="#21262D", fg="#58A6FF", hover="#30363D"))
        self.btn_refresh.clicked.connect(self._on_refresh)
        mlo.addWidget(self.btn_refresh)

        self.btn_save = QPushButton("Save")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.setStyleSheet(_SMALL_BTN.format(bg="#238636", fg="#FFFFFF", hover="#2EA043"))
        self.btn_save.clicked.connect(self._on_save)
        mlo.addWidget(self.btn_save)

        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_remove.setStyleSheet(_SMALL_BTN.format(bg="#382020", fg="#F85149", hover="#4D2828"))
        self.btn_remove.clicked.connect(lambda: self.remove_requested.emit(self.provider))
        mlo.addWidget(self.btn_remove)
        lo.addLayout(mlo)

    def _on_validate(self):
        key = self.inp_key.text().strip()
        base_url = self.inp_url.text().strip() if self.inp_url else ""
        if key or base_url:
            self.set_searching(True)
            self.validate_requested.emit(self.provider, key, base_url)

    def _on_show_key(self):
        """Request the engine to decrypt and return the stored key."""
        self.btn_show_key.setText("Loading…")
        self.btn_show_key.setEnabled(False)
        self.reveal_key_requested.emit(self.provider)

    def reveal_key(self, api_key: str):
        """Populate the input with the decrypted key in cleartext, auto-hide after 30s."""
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Normal)
        self.inp_key.setText(api_key)
        self.btn_show_key.setText("Hide Key")
        self.btn_show_key.setEnabled(True)
        self.btn_show_key.setStyleSheet(_SMALL_BTN.format(bg="#DC2626", fg="#FFFFFF", hover="#EF4444"))
        try:
            self.btn_show_key.clicked.disconnect()
        except RuntimeError:
            pass
        self.btn_show_key.clicked.connect(self._hide_key)
        self._reveal_timer.start(30_000)  # auto-hide after 30 seconds

    def _hide_key(self):
        """Re-mask the API key input and reset the Show Key button."""
        self._reveal_timer.stop()
        self.inp_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.inp_key.clear()
        self.inp_key.setPlaceholderText("••••••••••••••••")
        self.btn_show_key.setText("Show Key")
        self.btn_show_key.setEnabled(True)
        self.btn_show_key.setStyleSheet(_SMALL_BTN.format(bg="#7C3AED", fg="#FFFFFF", hover="#8B5CF6"))
        try:
            self.btn_show_key.clicked.disconnect()
        except RuntimeError:
            pass
        self.btn_show_key.clicked.connect(self._on_show_key)

    def _on_refresh(self):
        self.set_refreshing(True)
        self.refresh_requested.emit(self.provider)

    def set_refreshing(self, is_refreshing: bool):
        if is_refreshing:
            self.btn_refresh.setText("Refreshing…")
            self.btn_refresh.setEnabled(False)
        else:
            self.btn_refresh.setText("↻ Refresh")
            self.btn_refresh.setEnabled(True)

    def set_searching(self, is_searching: bool):
        if is_searching:
            self.btn_validate.setText("Searching…")
            self.btn_validate.setEnabled(False)
        else:
            self.btn_validate.setText("Test")
            self.btn_validate.setEnabled(True)

    def update_models(self, models: list):
        self.set_searching(False)
        if not models:
            return
        self.models = list(models)
        current = self.cmb_model.currentText()
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        self.cmb_model.addItems(self.models)
        if current in self.models:
            self.cmb_model.setCurrentText(current)
        elif self.models:
            self.cmb_model.setCurrentIndex(0)
        self.cmb_model.blockSignals(False)

    def _on_save(self):
        key = self.inp_key.text().strip()
        is_def = self.chk_default.isChecked()
        model = self.cmb_model.currentText().strip()
        base_url = self.inp_url.text().strip() if self.inp_url else ""
        self.save_requested.emit(self.provider, key, is_def, model, base_url)

    def set_stored_state(self, has_key: bool, is_default: bool, default_model: str, base_url: str = ""):
        if has_key:
            self.inp_key.setPlaceholderText("••••••••••••••••")
        if self.inp_url and base_url:
            self.inp_url.setText(base_url)
        self.chk_default.setChecked(is_default)
        if default_model and default_model in self.models:
            self.cmb_model.setCurrentText(default_model)
        elif self.models:
            self.cmb_model.setCurrentIndex(0)


class ModelsPanel(QWidget):
    """Models & Capabilities panel with Cloud and Local tabs, embedded within Settings Drawer."""

    validate_requested = Signal(str, str, str)           # provider, api_key, base_url
    save_requested = Signal(str, str, bool, str, str)    # provider, api_key, is_default, default_model, base_url
    remove_requested = Signal(str)
    refresh_models_requested = Signal(str)
    reveal_key_requested = Signal(str)                   # provider
    model_changed = Signal(str, str)
    download_local_model_requested = Signal(str, str) # model, dest_dir
    delete_local_model_requested = Signal(str)       # model
    set_capability_schedule_requested = Signal(str, str) # schedule, method
    run_capability_now_requested = Signal(str)       # method

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("models_panel")
        self._provider_rows: dict[str, ProviderRow] = {}
        self.local_items: dict[str, LocalModelItem] = {}
        self._build()

    def _build(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(12, 12, 12, 12)
        lo.setSpacing(10)

        # Title
        title = QLabel("Models & Capabilities")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #E2E8F0; background: transparent;")
        lo.addWidget(title)

        # Segmented control
        seg = QHBoxLayout()
        seg.setSpacing(4)
        self.btn_cloud = QPushButton("Cloud")
        self.btn_local = QPushButton("Local")
        for btn in (self.btn_cloud, self.btn_local):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(30)
        self.btn_cloud.setStyleSheet(_SEG_ACTIVE)
        self.btn_local.setStyleSheet(_SEG_INACTIVE)
        self.btn_cloud.clicked.connect(lambda: self._switch_tab("cloud"))
        self.btn_local.clicked.connect(lambda: self._switch_tab("local"))
        seg.addWidget(self.btn_cloud)
        seg.addWidget(self.btn_local)
        seg.addStretch()
        lo.addLayout(seg)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #1E293B;")
        lo.addWidget(sep)

        # Cloud tab
        self.cloud_scroll = QScrollArea()
        self.cloud_scroll.setWidgetResizable(True)
        self.cloud_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.cloud_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cloud_scroll.setStyleSheet(_SCROLL_CSS)
        self.cloud_scroll.verticalScrollBar().setSingleStep(16)

        self.cloud_content = QWidget()
        self.cloud_content.setStyleSheet("background: transparent;")
        self.cloud_layout = QVBoxLayout(self.cloud_content)
        self.cloud_layout.setContentsMargins(0, 0, 0, 0)
        self.cloud_layout.setSpacing(8)

        # Dynamic cloud providers container
        self.providers_container = QWidget()
        self.providers_container.setStyleSheet("background: transparent;")
        self.providers_layout = QVBoxLayout(self.providers_container)
        self.providers_layout.setContentsMargins(0, 0, 0, 0)
        self.providers_layout.setSpacing(4)
        self.cloud_layout.addWidget(self.providers_container)

        # Capabilities Check Row (placed only under Cloud section)
        self.cap_row = CapabilitiesCheckRow(self.cloud_content)
        self.cap_row.set_schedule_requested.connect(self.set_capability_schedule_requested.emit)
        self.cap_row.run_now_requested.connect(self.run_capability_now_requested.emit)
        self.cloud_layout.addWidget(self.cap_row)

        self.cloud_layout.addStretch()
        self.cloud_scroll.setWidget(self.cloud_content)
        lo.addWidget(self.cloud_scroll)

        # Local tab
        self.local_scroll = QScrollArea()
        self.local_scroll.setWidgetResizable(True)
        self.local_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.local_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.local_scroll.setStyleSheet(_SCROLL_CSS)
        self.local_scroll.verticalScrollBar().setSingleStep(16)

        self.local_content = QWidget()
        self.local_content.setStyleSheet("background: transparent;")
        self.local_layout = QVBoxLayout(self.local_content)
        self.local_layout.setContentsMargins(0, 0, 0, 0)
        self.local_layout.setSpacing(6)
        self.local_scroll.setWidget(self.local_content)
        self.local_scroll.setVisible(False)
        lo.addWidget(self.local_scroll)

    def _on_download_clicked(self, name: str):
        default_dir = str(Path.home() / ".ollama" / "models")
        dir_path = QFileDialog.getExistingDirectory(self, f"Select Directory to Download {name}", default_dir)
        if dir_path:
            self.download_local_model_requested.emit(name, dir_path)

    def populate_local_models(self, models: list):
        while self.local_layout.count() > 0:
            item = self.local_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.local_items.clear()

        for m in models:
            item = LocalModelItem(m["name"], m["size"], m.get("downloaded", False), m.get("path", ""), self.local_content)
            item.download_clicked.connect(self._on_download_clicked)
            item.delete_clicked.connect(self.delete_local_model_requested.emit)
            self.local_layout.addWidget(item)
            self.local_items[m["name"]] = item

    def update_download_progress(self, model_name: str, percent: float):
        if model_name in self.local_items:
            self.local_items[model_name].set_progress(percent)

    def populate_providers(self, providers: list):
        while self.providers_layout.count() > 0:
            item = self.providers_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._provider_rows.clear()

        for p in providers:
            row = ProviderRow(
                name=p["name"],
                display_name=p["display_name"],
                icon=p["icon"],
                key_url=p["key_url"],
                models=p.get("models", []),
                supports_custom_url=p.get("supports_custom_url", False),
                base_url=p.get("base_url", ""),
                parent=self.providers_container,
            )
            row.toggled.connect(self._on_row_toggled)
            row.detail.validate_requested.connect(self.validate_requested)
            row.detail.save_requested.connect(self.save_requested)
            row.detail.remove_requested.connect(self.remove_requested)
            row.refresh_requested.connect(self.refresh_models_requested.emit)
            row.reveal_key_requested.connect(self.reveal_key_requested.emit)
            if p.get("models"):
                row.detail.cmb_model.currentTextChanged.connect(
                    lambda model, pname=p["name"]: self.model_changed.emit(pname, model)
                )

            status = "connected" if p.get("has_key") else "no_key"
            row.set_badge(status)
            row.detail.set_stored_state(
                has_key=p.get("has_key", False),
                is_default=p.get("is_default", False),
                default_model=p.get("default_model", ""),
                base_url=p.get("base_url", ""),
            )

            self.providers_layout.addWidget(row)
            self._provider_rows[p["name"]] = row

    def update_provider_models(self, provider_name: str, models: list):
        if provider_name in self._provider_rows:
            self._provider_rows[provider_name].update_models(models)

    def _on_row_toggled(self, name: str, expanding: bool):
        if expanding:
            for rname, row in self._provider_rows.items():
                if rname != name and row.is_expanded:
                    row.toggle_expand()

    def _switch_tab(self, tab: str):
        is_cloud = tab == "cloud"
        self.btn_cloud.setStyleSheet(_SEG_ACTIVE if is_cloud else _SEG_INACTIVE)
        self.btn_local.setStyleSheet(_SEG_INACTIVE if is_cloud else _SEG_ACTIVE)
        self.cloud_scroll.setVisible(is_cloud)
        self.local_scroll.setVisible(not is_cloud)
