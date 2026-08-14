"""Main PySide6 Window: integrated frameless border, glowing Orb, HUD stream, persistent window geometry memory, and linked Settings modal overlay with background blur."""
import asyncio
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import sys

import qasync
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aether_common.contracts import Event, EventType
from aether_engine.config import load_config, save_config
from aether_ui.components.approval_drawer import ApprovalDrawer
from aether_ui.settings_window import AetherConfigWindow
from aether_ui.hud import HudWidget
from aether_ui.orb import OrbWidget
from aether_ui.state import UIStateMachine
from aether_ui.theme import (
    BRAND_ENGINE,
    BRAND_FRONTEND,
    STATUS_OFFLINE,
    SURFACE_BG,
    SURFACE_BORDER,
    SURFACE_PANEL,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from aether_ui.ws_client import AetherWSClient

logger = logging.getLogger("aether_ui.main")


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", _POINT),
        ("ptMaxSize", _POINT),
        ("ptMaxPosition", _POINT),
        ("ptMinTrackSize", _POINT),
        ("ptMaxTrackSize", _POINT),
    ]


_TB_BTN = """QPushButton {{
    background: transparent; color: #8A8FA3; border: none; font-size: {fs}px; font-weight: bold;
}} QPushButton:hover {{ background: {hover}; color: #FFFFFF; border-radius: 4px; }}"""

_MIN_CHAT_W = 440
_MAX_WINDOW_W = 1400
_MIN_WINDOW_H = 540
_MAX_WINDOW_H = 1000


class AetherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        # Load persisted config for window geometry memory
        self.user_config = load_config()
        screen = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        screen_w = screen.width() if screen else 1920
        screen_h = screen.height() if screen else 1080

        start_w = self.user_config.window_width or 520
        start_h = self.user_config.window_height or 740
        if start_w >= screen_w or start_h >= screen_h:
            start_w = min(540, screen_w - 100)
            start_h = min(740, screen_h - 100)

        start_w = max(_MIN_CHAT_W, start_w)
        start_h = max(_MIN_WINDOW_H, start_h)

        self.resize(start_w, start_h)
        self.setMinimumSize(_MIN_CHAT_W, _MIN_WINDOW_H)
        self._normal_geometry = self.geometry()

        # State machine + WS client
        self._current_default_provider = self.user_config.default_provider or "google_gemini"
        self.sm = UIStateMachine()
        self.ws_client = AetherWSClient(
            state_machine=self.sm,
            on_event=self._on_ws_event,
        )

        self._drag = QPoint()
        self._build_ui()

        # Connect state machine updates to Orb
        self.sm.subscribe(self._on_ui_state_changed)
        self._on_ui_state_changed(self.sm)

    def _on_ui_state_changed(self, sm: UIStateMachine):
        """Update Orb state and visual indicators whenever UIState transitions."""
        if hasattr(self, "orb") and self.orb is not None:
            self.orb.set_state(sm.state)

    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Container widget for Main HUD (to apply QGraphicsBlurEffect cleanly)
        self.hud_container = QWidget(self)
        root_layout.addWidget(self.hud_container)

        ml = QVBoxLayout(self.hud_container)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        # Title bar matching Ionized Void specifications
        tb = QWidget()
        tb.setFixedHeight(40)
        tb.setStyleSheet(f"background: {SURFACE_BG}; border-bottom: 1px solid {SURFACE_BORDER};")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(10, 0, 10, 0)
        tl.setSpacing(6)

        # Gear button for Settings Modal
        self.btn_gear = QPushButton("⚙")
        self.btn_gear.setFixedSize(30, 30)
        self.btn_gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gear.setToolTip("Settings")
        self.btn_gear.setStyleSheet(_TB_BTN.format(fs=14, hover=SURFACE_PANEL))
        self.btn_gear.clicked.connect(self._toggle_settings)
        tl.addWidget(self.btn_gear)

        dot = QLabel("●")
        dot.setStyleSheet(f"color:{BRAND_FRONTEND}; font-size:10px; margin-left:2px;")
        tl.addWidget(dot)

        title = QLabel("AETHER")
        title.setStyleSheet(f"font-weight:700; font-size:12px; color:{TEXT_PRIMARY}; font-family:'Segoe UI', sans-serif; letter-spacing:1px;")
        tl.addWidget(title)

        tl.addStretch()

        self.btn_min = QPushButton("—")
        self.btn_min.setFixedSize(30, 30)
        self.btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_min.setStyleSheet(_TB_BTN.format(fs=13, hover=SURFACE_PANEL))
        self.btn_min.clicked.connect(self._minimize_window)
        tl.addWidget(self.btn_min)

        self.btn_max = QPushButton("🗖")
        self.btn_max.setFixedSize(30, 30)
        self.btn_max.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_max.setStyleSheet(_TB_BTN.format(fs=13, hover=SURFACE_PANEL))
        self.btn_max.clicked.connect(self._toggle_maximize)
        tl.addWidget(self.btn_max)

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet(_TB_BTN.format(fs=12, hover=STATUS_OFFLINE))
        self.btn_close.clicked.connect(self.close)
        tl.addWidget(self.btn_close)
        ml.addWidget(tb)

        # Main content column (orb + hud)
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)

        # Orb visualizer container
        oc = QWidget()
        oc.setStyleSheet("background:transparent;")
        ol = QVBoxLayout(oc)
        ol.setContentsMargins(0, 12, 0, 4)
        ol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.orb = OrbWidget(self, 190)
        ol.addWidget(self.orb)
        main_col.addWidget(oc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{SURFACE_BORDER};")
        main_col.addWidget(sep)

        # HUD Stream Widget
        self.hud = HudWidget(self.sm, self)
        self.hud.start_task_requested.connect(
            lambda prompt, model: asyncio.create_task(self.ws_client.start_task(prompt, model))
        )
        self.hud.cancel_task_requested.connect(
            lambda: asyncio.create_task(self.ws_client.cancel_task())
        )
        self.hud.model_changed_by_user.connect(
            lambda model: asyncio.create_task(self.ws_client.set_default_model(self._current_default_provider, model))
        )
        main_col.addWidget(self.hud)

        ml.addLayout(main_col, 1)

        # Settings Modal Overlay (Linked to main window with blur backdrop)
        self.config_modal = AetherConfigWindow(self)
        self.config_modal.hide()
        self.config_modal.closed.connect(self._on_settings_closed)

        self.config_modal.validate_requested.connect(
            lambda p, k, u="": asyncio.create_task(self.ws_client.validate_provider(p, k, base_url=u))
        )
        self.config_modal.save_requested.connect(
            lambda p, k, d, m, u="", dn="", pt="": asyncio.create_task(self.ws_client.save_provider(p, k, d, m, base_url=u, display_name=dn, provider_type=pt))
        )
        self.config_modal.remove_requested.connect(
            lambda p: asyncio.create_task(self.ws_client.remove_provider(p))
        )
        self.config_modal.refresh_models_requested.connect(
            lambda p: asyncio.create_task(self.ws_client.refresh_models(p))
        )
        self.config_modal.reveal_key_requested.connect(
            lambda p: asyncio.create_task(self.ws_client.reveal_provider_key(p))
        )
        self.config_modal.download_local_model_requested.connect(
            lambda m, d: asyncio.create_task(self.ws_client.start_local_model_download(m, d))
        )
        self.config_modal.delete_local_model_requested.connect(
            lambda m: asyncio.create_task(self.ws_client.delete_local_model(m))
        )

        # Tool Approval Drawer
        self.approval_drawer = ApprovalDrawer(self)
        self.approval_drawer.hide()
        self.approval_drawer.approve_btn.clicked.connect(
            lambda: asyncio.create_task(self.ws_client.send_approval(True, self.approval_drawer._approval_key, override_class=self.approval_drawer.get_selected_class()))
        )
        self.approval_drawer.reject_btn.clicked.connect(
            lambda: asyncio.create_task(self.ws_client.send_approval(False, self.approval_drawer._approval_key))
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(SURFACE_BG))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "config_modal") and self.config_modal.isVisible():
            self.config_modal.update_geometry()
        if hasattr(self, "approval_drawer") and self.approval_drawer.isVisible():
            self.approval_drawer.update_geometry()
        self._save_window_geometry()

    @property
    def is_maximized_or_fullscreen(self) -> bool:
        """Helper to determine if the window is in a maximized or fullscreen state."""
        return bool(self.windowState() & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen))

    def _save_window_geometry(self):
        """Persist current window dimensions to ~/.aether/config.json."""
        if not self.isMinimized() and not self.is_maximized_or_fullscreen:
            screen = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
            if screen is None or (self.width() < screen.width() and self.height() < screen.height()):
                if self.width() >= _MIN_CHAT_W and self.height() >= _MIN_WINDOW_H:
                    self.user_config.window_width = self.width()
                    self.user_config.window_height = self.height()
                    save_config(self.user_config)

    def _toggle_settings(self):
        if self.config_modal.isVisible():
            self.config_modal.close_modal()
        else:
            # Apply blur effect on the main HUD container
            blur = QGraphicsBlurEffect(self)
            blur.setBlurRadius(16)
            self.hud_container.setGraphicsEffect(blur)

            # Show modal overlay centered over main window
            self.config_modal.update_geometry()
            self.config_modal.show()
            self.config_modal.raise_()
            self.config_modal.setFocus()

            asyncio.create_task(self.ws_client.request_provider_list())
            asyncio.create_task(self.ws_client.request_local_models_list())

    def _on_settings_closed(self):
        """Remove blur effect when settings modal closes."""
        self.hud_container.setGraphicsEffect(None)

    def _on_ws_event(self, event: Event):
        """Dispatch incoming WebSocket events to HUD and Settings modal."""
        self.hud.log_event(event)

        p = event.payload or {}
        t = event.type

        if t in (EventType.PROVIDER_LIST_RESPONSE, EventType.REFRESH_MODELS_RESPONSE):
            providers = p.get("providers", [])
            default_provider = p.get("default_provider", "google_gemini")
            default_model = p.get("default_model", "")
            self._current_default_provider = default_provider

            if self.config_modal is not None:
                self.config_modal.populate_providers(providers)

            models = []
            for prov in providers:
                if prov.get("is_default") or prov.get("name") == self._current_default_provider:
                    models = prov.get("models", [])
                    break

            if not models and providers:
                for prov in providers:
                    if prov.get("has_key") and prov.get("models"):
                        self._current_default_provider = prov.get("name")
                        models = prov.get("models")
                        break
                if not models:
                    models = providers[0].get("models", [])

            if models:
                self.hud.set_available_models(models)
                target_model = default_model if (default_model and default_model in models) else models[0]
            else:
                self.hud.set_available_models([])
                target_model = default_model or "No Model"
                
            self.hud.set_active_model(self._current_default_provider, target_model)

        elif t in (EventType.PROVIDER_VALIDATE_RESPONSE, EventType.SETTINGS_PROVIDER_VALIDATE_RESULT):
            provider_name = p.get("provider", "")
            models = p.get("models", [])
            status = p.get("status", "no_key")
            latency = p.get("latency_ms", 0.0)

            if self.config_modal is not None and provider_name in self.config_modal._provider_cards:
                card = self.config_modal._provider_cards[provider_name]
                card.set_badge(status)
                if models:
                    card.update_models(models)
                    if card.chk_default.isChecked() or getattr(card, "is_default", False):
                        self.hud.set_available_models(models)
            elif models:
                self.hud.set_available_models(models)

            if self.config_modal is not None and latency > 0:
                self.config_modal.update_latency(latency)

        elif t == EventType.PROVIDER_SAVE_RESPONSE:
            provider_name = p.get("provider", "")
            if p.get("success"):
                if self.config_modal is not None and provider_name in self.config_modal._provider_cards:
                    card = self.config_modal._provider_cards[provider_name]
                    card.on_saved_success()
                asyncio.create_task(self.ws_client.request_provider_list())

        elif t == EventType.PROVIDER_REVEAL_KEY_RESPONSE:
            provider_name = p.get("provider", "")
            if self.config_modal is not None and p.get("success") and provider_name in self.config_modal._provider_cards:
                card = self.config_modal._provider_cards[provider_name]
                card.inp_key.setText(p.get("api_key", ""))

        elif t == EventType.TOOL_APPROVAL_REQUEST:
            self.approval_drawer.show_for_request(p)
            self.approval_drawer.setFocus()

        elif t in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED, EventType.WORKER_EXECUTION_COMPLETED):
            self.approval_drawer.hide_drawer()

    def _minimize_window(self):
        self._drag = QPoint()
        QApplication.restoreOverrideCursor()
        self.showMinimized()

    def _toggle_maximize(self):
        self._drag = QPoint()
        QApplication.restoreOverrideCursor()
        if self.is_maximized_or_fullscreen:
            self.showNormal()
            if hasattr(self, "_normal_geometry") and self._normal_geometry and not self._normal_geometry.isEmpty():
                self.setGeometry(self._normal_geometry)
            self.btn_max.setText("🗖")
        else:
            self._normal_geometry = self.geometry()
            self.showMaximized()
            self.btn_max.setText("🗗")

    def _toggle_fullscreen(self):
        self._drag = QPoint()
        QApplication.restoreOverrideCursor()
        if self.isFullScreen():
            self.showNormal()
            if hasattr(self, "_normal_geometry") and self._normal_geometry and not self._normal_geometry.isEmpty():
                self.setGeometry(self._normal_geometry)
        else:
            if not self.is_maximized_or_fullscreen:
                self._normal_geometry = self.geometry()
            self.showFullScreen()

    def changeEvent(self, e):
        """Single source of truth for the maximize button icon."""
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.Type.WindowStateChange and hasattr(self, "btn_max"):
            if self.is_maximized_or_fullscreen:
                self.btn_max.setText("🗗")
            else:
                self.btn_max.setText("🗖")
        super().changeEvent(e)

    # Frameless drag & double-click titlebar
    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= 40:
            pt = e.position().toPoint()
            child = self.childAt(pt)
            if child is None or child not in (
                getattr(self, "btn_gear", None),
                getattr(self, "btn_min", None),
                getattr(self, "btn_max", None),
                getattr(self, "btn_close", None),
            ):
                self._toggle_maximize()
                return
        super().mouseDoubleClickEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= 40:
            if not self.is_maximized_or_fullscreen:
                pt = e.position().toPoint()
                child = self.childAt(pt)
                if child is None or child not in (
                    getattr(self, "btn_gear", None),
                    getattr(self, "btn_min", None),
                    getattr(self, "btn_max", None),
                    getattr(self, "btn_close", None),
                ):
                    self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and not self._drag.isNull():
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = QPoint()
        super().mouseReleaseEvent(e)

    def nativeEvent(self, eventType, message):
        if sys.platform == "win32" and eventType == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(message.__int__())

            if msg.message == 0x0024:  # WM_GETMINMAXINFO
                info = _MINMAXINFO.from_address(msg.lParam)
                dpr = self.devicePixelRatio()
                info.ptMinTrackSize.x = int(_MIN_CHAT_W * dpr)
                info.ptMinTrackSize.y = int(_MIN_WINDOW_H * dpr)
                return True, 0

            if msg.message == 0x0084:  # WM_NCHITTEST
                pt = self.mapFromGlobal(self.cursor().pos())
                w, h = self.width(), self.height()

                if pt.x() < 0 or pt.x() > w or pt.y() < 0 or pt.y() > h:
                    return super().nativeEvent(eventType, message)

                child = self.childAt(pt)
                if child is not None:
                    if child in (getattr(self, "btn_gear", None), getattr(self, "btn_min", None), getattr(self, "btn_max", None), getattr(self, "btn_close", None)):
                        return True, 1  # HTCLIENT

                if self.is_maximized_or_fullscreen:
                    return True, 1  # HTCLIENT

                margin = 8
                left = pt.x() < margin
                right = pt.x() >= w - margin
                top = pt.y() < margin
                bottom = pt.y() >= h - margin

                if top and left: return True, 13
                if top and right: return True, 14
                if bottom and left: return True, 16
                if bottom and right: return True, 17
                if left: return True, 10
                if right: return True, 11
                if top: return True, 12
                if bottom: return True, 15

        return super().nativeEvent(eventType, message)

    def closeEvent(self, e):
        self._save_window_geometry()
        if hasattr(self, "orb") and self.orb is not None:
            self.orb.stop()
        if hasattr(self, "config_modal") and self.config_modal is not None:
            self.config_modal.close()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.ws_client.disconnect())
        except RuntimeError:
            pass
        super().closeEvent(e)


def run_ui():
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = AetherWindow()
    win.show()
    win.ws_client.start_background_listener()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    run_ui()
