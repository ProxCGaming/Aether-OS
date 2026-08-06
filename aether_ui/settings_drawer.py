"""Modern overlay Settings & Models Drawer that slides over the main window content.
Supports interactive edge resizing via mouse drag, outside-click to close, Escape key,
and full tabbed settings views without splitting the main window layout.
"""
from pathlib import Path
from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Property,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aether_ui.models_panel import ModelsPanel

_DEFAULT_DRAWER_WIDTH = 460
_MIN_DRAWER_WIDTH = 320
_EDGE_MARGIN = 8

_NAV_BTN_ACTIVE = """QPushButton {
    background: #1E293B; color: #38BDF8; font-size: 12px; font-weight: 600;
    padding: 6px 12px; border: 1px solid #38BDF8; border-radius: 6px; text-align: center;
}"""

_NAV_BTN_INACTIVE = """QPushButton {
    background: #0D1219; color: #94A3B8; font-size: 12px; font-weight: 500;
    padding: 6px 12px; border: 1px solid #1E293B; border-radius: 6px; text-align: center;
} QPushButton:hover { background: #161F2E; color: #E2E8F0; border-color: #475569; }"""

_CLOSE_BTN = """QPushButton {
    background: transparent; color: #94A3B8; border: none; font-size: 14px;
    font-weight: 700; border-radius: 4px; padding: 4px 8px;
} QPushButton:hover { background: #EF4444; color: #FFFFFF; }"""

_CARD_STYLE = """QFrame {
    background: #111827; border: 1px solid #1E293B; border-radius: 8px; padding: 12px;
}"""

_SCROLL_CSS = """
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical {
    background: #0D1219; width: 6px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #1E293B; min-height: 20px; border-radius: 3px;
}
QScrollBar::handle:vertical:hover { background: #38BDF8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class SettingsDrawer(QWidget):
    """Overlay drawer that slides over the main window content.
    
    Contains the ModelsPanel and other configuration sections.
    Supports interactive right-edge resizing and closes smoothly when clicking outside.
    """
    opened = Signal()
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settings_overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._is_open = False
        self._animating = False
        self._resizing = False
        self._hover_edge = False
        self._drag_start_x = 0
        self._drag_start_w = _DEFAULT_DRAWER_WIDTH
        self._panel_w = _DEFAULT_DRAWER_WIDTH
        self._drawer_x = -_DEFAULT_DRAWER_WIDTH
        self._backdrop_alpha = 0.0

        self._build_ui()
        self.setVisible(False)

    # Animated properties
    def _get_drawer_x(self) -> int:
        return self._drawer_x

    def _set_drawer_x(self, x: int):
        self._drawer_x = x
        self.panel.move(x, 0)
        self.update()

    drawerX = Property(int, _get_drawer_x, _set_drawer_x)

    def _get_backdrop_alpha(self) -> float:
        return self._backdrop_alpha

    def _set_backdrop_alpha(self, a: float):
        self._backdrop_alpha = a
        self.update()

    backdropAlpha = Property(float, _get_backdrop_alpha, _set_backdrop_alpha)

    def _build_ui(self):
        # The main drawer panel frame
        self.panel = QFrame(self)
        self.panel.setObjectName("drawer_panel")
        self.panel.setStyleSheet("QFrame#drawer_panel { background: #0D1219; border-right: 1px solid #1E293B; }")
        self.panel.setFixedWidth(self._panel_w)
        self.panel.setMouseTracking(True)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(10)

        # Header bar
        header_bar = QHBoxLayout()
        header_bar.setContentsMargins(4, 2, 4, 4)

        icon_title = QLabel("⚙  Settings")
        icon_title.setStyleSheet("font-size: 16px; font-weight: 700; color: #38BDF8; background: transparent;")
        header_bar.addWidget(icon_title)
        header_bar.addStretch()

        self.btn_close = QPushButton("✕")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setToolTip("Close Settings (Esc)")
        self.btn_close.setStyleSheet(_CLOSE_BTN)
        self.btn_close.clicked.connect(self.close)
        header_bar.addWidget(self.btn_close)
        panel_layout.addLayout(header_bar)

        # Navigation section selector pills
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(6)
        self.nav_btns: dict[str, QPushButton] = {}
        
        sections = [
            ("models", "🤖 Models"),
            ("tools", "🛠 Tools"),
            ("security", "🔒 Security"),
            ("about", "ℹ About"),
        ]

        for sec_id, sec_label in sections:
            b = QPushButton(sec_label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_NAV_BTN_ACTIVE if sec_id == "models" else _NAV_BTN_INACTIVE)
            b.clicked.connect(lambda _, s=sec_id: self._switch_section(s))
            nav_layout.addWidget(b)
            self.nav_btns[sec_id] = b

        panel_layout.addLayout(nav_layout)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #1E293B;")
        panel_layout.addWidget(sep)

        # Stacked pages
        self.stack = QStackedWidget()

        # Page 0: ModelsPanel
        self.models_panel = ModelsPanel(self.panel)
        self.stack.addWidget(self.models_panel)

        # Page 1: Tools view
        self.tools_page = self._build_tools_page()
        self.stack.addWidget(self.tools_page)

        # Page 2: Security view
        self.security_page = self._build_security_page()
        self.stack.addWidget(self.security_page)

        # Page 3: About view
        self.about_page = self._build_about_page()
        self.stack.addWidget(self.about_page)

        panel_layout.addWidget(self.stack)

    def _build_tools_page(self) -> QWidget:
        container = QWidget()
        scroll = QScrollArea(container)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLL_CSS)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        title = QLabel("Registered Agent Tools")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #E2E8F0; background: transparent;")
        layout.addWidget(title)

        tools = [
            ("⚡ Terminal Sandbox", "run_command", "Executes PowerShell commands within a safety-monitored local process sandbox with user confirmation for modifications."),
            ("📁 File System Access", "read_file / write_file", "Reads workspace files, views structured directories, and applies non-destructive file edits."),
            ("🔍 Web Retrieval", "search_web", "Performs real-time web queries using DuckDuckGo / Bing search endpoints."),
            ("❓ User Clarifications", "ask_question", "Presents interactive multi-choice prompts to clarify user requirements during orchestration."),
        ]

        for name, fn, desc in tools:
            card = QFrame()
            card.setStyleSheet(_CARD_STYLE)
            clo = QVBoxLayout(card)
            clo.setSpacing(4)
            h = QLabel(f"{name} <span style='color:#38BDF8; font-size:11px;'>({fn})</span>")
            h.setStyleSheet("font-size: 13px; font-weight: 600; color: #F1F5F9; background: transparent;")
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("font-size: 11px; color: #94A3B8; background: transparent; line-height: 1.4;")
            clo.addWidget(h)
            clo.addWidget(d)
            layout.addWidget(card)

        layout.addStretch()
        scroll.setWidget(content)

        main_lo = QVBoxLayout(container)
        main_lo.setContentsMargins(0, 0, 0, 0)
        main_lo.addWidget(scroll)
        return container

    def _build_security_page(self) -> QWidget:
        container = QWidget()
        scroll = QScrollArea(container)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(_SCROLL_CSS)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        title = QLabel("Security & DPAPI Keystore")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #E2E8F0; background: transparent;")
        layout.addWidget(title)

        sec_items = [
            ("🔐 Windows DPAPI Keystore", "Active (`~/.aether/secrets.db`)", "API keys and sensitive tokens are encrypted using Windows DPAPI user-tied entropy. Zero plaintext keys are ever saved to disk."),
            ("🛡 Local Engine Sandbox", "Port 8765 (`127.0.0.1`)", "WebSocket connections require authenticated HMAC tokens. The engine only binds to localhost."),
            ("⚙ User Settings", "Active (`~/.aether/config.json`)", "Window geometry and selected default models are preserved in local JSON configuration."),
        ]

        for name, badge, desc in sec_items:
            card = QFrame()
            card.setStyleSheet(_CARD_STYLE)
            clo = QVBoxLayout(card)
            clo.setSpacing(4)
            h = QLabel(f"{name} — <span style='color:#10B981; font-size:11px; font-weight:600;'>{badge}</span>")
            h.setStyleSheet("font-size: 13px; font-weight: 600; color: #F1F5F9; background: transparent;")
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("font-size: 11px; color: #94A3B8; background: transparent; line-height: 1.4;")
            clo.addWidget(h)
            clo.addWidget(d)
            layout.addWidget(card)

        layout.addStretch()
        scroll.setWidget(content)

        main_lo = QVBoxLayout(container)
        main_lo.setContentsMargins(0, 0, 0, 0)
        main_lo.addWidget(scroll)
        return container

    def _build_about_page(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(12)

        app_title = QLabel("AETHER v2.0")
        app_title.setStyleSheet("font-size: 18px; font-weight: 800; color: #38BDF8; background: transparent;")
        layout.addWidget(app_title)

        sub = QLabel("Local Agentic AI Desktop Assistant")
        sub.setStyleSheet("font-size: 12px; color: #94A3B8; background: transparent;")
        layout.addWidget(sub)

        card = QFrame()
        card.setStyleSheet(_CARD_STYLE)
        clo = QVBoxLayout(card)
        clo.setSpacing(8)

        info_lines = [
            ("Core Architecture", "PySide6 HUD + FastAPI Engine + LiteLLM"),
            ("Routing Engine", "Reachability Filtered Cost & Capability Routing"),
            ("Communication", "Authenticated Local WebSocket (`/ws/tasks`)"),
            ("Telemetry", "Zero Cloud Telemetry • 100% Local Execution"),
        ]

        for label, val in info_lines:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setStyleSheet("font-size: 12px; color: #94A3B8; font-weight: 500; background: transparent;")
            v = QLabel(val)
            v.setStyleSheet("font-size: 12px; color: #F1F5F9; font-weight: 600; background: transparent;")
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(v)
            clo.addLayout(row)

        layout.addWidget(card)
        layout.addStretch()
        return container

    def _switch_section(self, sec_id: str):
        sec_map = {"models": 0, "tools": 1, "security": 2, "about": 3}
        idx = sec_map.get(sec_id, 0)
        self.stack.setCurrentIndex(idx)

        for sid, btn in self.nav_btns.items():
            btn.setStyleSheet(_NAV_BTN_ACTIVE if sid == sec_id else _NAV_BTN_INACTIVE)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Draw semi-transparent backdrop behind the drawer panel
        if self._backdrop_alpha > 0.0:
            alpha = int(150 * min(1.0, max(0.0, self._backdrop_alpha)))
            painter.fillRect(self.rect(), QColor(0, 0, 0, alpha))

        # 2. Draw resizable edge divider line & grip pill when open
        if self._is_open:
            edge_x = self._panel_w - 1
            line_color = QColor("#38BDF8") if (self._resizing or self._hover_edge) else QColor("#1E293B")
            painter.setPen(line_color)
            painter.drawLine(edge_x, 0, edge_x, self.height())

            grip_color = QColor("#38BDF8") if self._resizing else (QColor("#64748B") if self._hover_edge else QColor("#334155"))
            grip_w, grip_h = 3, 32
            grip_x = self._panel_w - 4
            grip_y = (self.height() - grip_h) // 2
            painter.setBrush(grip_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(grip_x, grip_y, grip_w, grip_h, 1.5, 1.5)

    def update_geometry(self):
        """Update overlay geometry to match parent window below the title bar."""
        if self.parent():
            p_w = self.parent().width()
            p_h = self.parent().height()
            top_bar_h = 36
            self.setGeometry(0, top_bar_h, p_w, max(100, p_h - top_bar_h))
            max_w = max(_MIN_DRAWER_WIDTH, p_w - 30)
            self._panel_w = min(self._panel_w, max_w)
            self.panel.setFixedWidth(self._panel_w)
            self.panel.setFixedHeight(self.height())
            if not self._is_open and not self._animating:
                self._drawer_x = -self._panel_w
                self.panel.move(self._drawer_x, 0)

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self):
        if self._is_open:
            return
        self.update_geometry()
        self.setVisible(True)
        self.raise_()
        self._is_open = True
        self._animating = True

        anim_group = QParallelAnimationGroup(self)

        # Slide in animation
        slide_anim = QPropertyAnimation(self, b"drawerX")
        slide_anim.setDuration(220)
        slide_anim.setStartValue(-self._panel_w)
        slide_anim.setEndValue(0)
        slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim_group.addAnimation(slide_anim)

        # Fade in backdrop
        fade_anim = QPropertyAnimation(self, b"backdropAlpha")
        fade_anim.setDuration(220)
        fade_anim.setStartValue(0.0)
        fade_anim.setEndValue(1.0)
        fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim_group.addAnimation(fade_anim)

        def _on_opened():
            self._animating = False
            self.opened.emit()

        anim_group.finished.connect(_on_opened)
        self._anim_group = anim_group
        anim_group.start()

    def close(self):
        if not self._is_open:
            return
        self._animating = True
        self._is_open = False
        self._resizing = False
        self._hover_edge = False
        self.unsetCursor()

        anim_group = QParallelAnimationGroup(self)

        # Slide out animation
        slide_anim = QPropertyAnimation(self, b"drawerX")
        slide_anim.setDuration(180)
        slide_anim.setStartValue(self._drawer_x)
        slide_anim.setEndValue(-self._panel_w)
        slide_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim_group.addAnimation(slide_anim)

        # Fade out backdrop
        fade_anim = QPropertyAnimation(self, b"backdropAlpha")
        fade_anim.setDuration(180)
        fade_anim.setStartValue(self._backdrop_alpha)
        fade_anim.setEndValue(0.0)
        fade_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim_group.addAnimation(fade_anim)

        def _on_closed():
            self._animating = False
            self.setVisible(False)
            self.closed.emit()

        anim_group.finished.connect(_on_closed)
        self._anim_group = anim_group
        anim_group.start()

    def toggle(self):
        if self._is_open:
            self.close()
        else:
            self.open()

    # Edge resizing & click-outside handling
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._is_open:
            pos = event.position().toPoint()
            # If pressed on the right edge handle, start resizing
            if abs(pos.x() - self._panel_w) <= _EDGE_MARGIN:
                self._resizing = True
                self._drag_start_x = event.globalPosition().x()
                self._drag_start_w = self._panel_w
                self.update()
                event.accept()
                return

            # If clicked on the backdrop area outside the drawer panel, close
            if pos.x() > self._panel_w + _EDGE_MARGIN:
                self.close()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_open:
            x = event.position().x()
            near_edge = abs(x - self._panel_w) <= _EDGE_MARGIN

            if self._resizing:
                delta = event.globalPosition().x() - self._drag_start_x
                max_w = max(_MIN_DRAWER_WIDTH, self.width() - 30) if self.parent() else 900
                new_w = int(max(_MIN_DRAWER_WIDTH, min(max_w, self._drag_start_w + delta)))
                self._panel_w = new_w
                self.panel.setFixedWidth(new_w)
                self.update()
                event.accept()
                return
            elif near_edge:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                if not self._hover_edge:
                    self._hover_edge = True
                    self.update()
                event.accept()
                return
            else:
                if self._hover_edge:
                    self._hover_edge = False
                    self.unsetCursor()
                    self.update()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            self.unsetCursor()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if self._hover_edge:
            self._hover_edge = False
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        """Close drawer if user presses Escape key."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)
