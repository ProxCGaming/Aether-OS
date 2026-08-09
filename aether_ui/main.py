"""Main PySide6 Window: integrated frameless border, glowing Orb, HUD stream, persistent window geometry memory, and resizable Models & Settings drawer."""
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
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aether_common.contracts import Event, EventType
from aether_engine.config import load_config, save_config
from aether_ui.components.approval_drawer import ApprovalDrawer
from aether_ui.hud import HudWidget
from aether_ui.models_panel import ModelsPanel
from aether_ui.orb import OrbWidget
from aether_ui.settings_drawer import SettingsDrawer
from aether_ui.state import UIStateMachine
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
    background: transparent; color: #8B949E; border: none; font-size: {fs}px;
}} QPushButton:hover {{ background: {hover}; color: #FFFFFF; }}"""

_MIN_CHAT_W = 380
_MAX_WINDOW_W = 1400
_MIN_WINDOW_H = 500
_MAX_WINDOW_H = 1000


class AetherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        # Load persisted config for window geometry memory
        self.user_config = load_config()
        start_w = max(420, self.user_config.window_width)
        start_h = max(_MIN_WINDOW_H, self.user_config.window_height)

        self.resize(start_w, start_h)
        self.setMinimumSize(420, _MIN_WINDOW_H)
        self.setMaximumSize(_MAX_WINDOW_W, _MAX_WINDOW_H)

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
        ml = QVBoxLayout(self)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        # Title bar (gear icon + title + min/close)
        tb = QWidget()
        tb.setFixedHeight(36)
        tb.setStyleSheet("background: #090D12; border-bottom: 1px solid #1E293B;")
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(8, 0, 8, 0)
        tl.setSpacing(4)

        # Gear button
        self.btn_gear = QPushButton("⚙")
        self.btn_gear.setFixedSize(28, 28)
        self.btn_gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gear.setToolTip("Settings & Models")
        self.btn_gear.setStyleSheet(_TB_BTN.format(fs=14, hover="#1E293B"))
        self.btn_gear.clicked.connect(self._toggle_settings)
        tl.addWidget(self.btn_gear)

        title = QLabel("AETHER")
        title.setStyleSheet("font-weight:700; font-size:12px; color:#38BDF8;")
        tl.addWidget(title)
        tl.addStretch()
        self.btn_min = QPushButton("🗕")
        self.btn_min.setFixedSize(28, 28)
        self.btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_min.setStyleSheet(_TB_BTN.format(fs=13, hover="#1E293B"))
        self.btn_min.clicked.connect(self._minimize_window)
        tl.addWidget(self.btn_min)

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet(_TB_BTN.format(fs=12, hover="#EF4444"))
        self.btn_close.clicked.connect(self.close)
        tl.addWidget(self.btn_close)
        ml.addWidget(tb)

        # Main content column (orb + hud)
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)

        # Orb
        oc = QWidget()
        oc.setStyleSheet("background:transparent;")
        ol = QVBoxLayout(oc)
        ol.setContentsMargins(0, 16, 0, 8)
        ol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.orb = OrbWidget(self, 170)
        ol.addWidget(self.orb)
        main_col.addWidget(oc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#1E293B;")
        main_col.addWidget(sep)

        # HUD
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

        # Overlay Settings & Models Drawer (appears OVER all content)
        self.drawer = SettingsDrawer(self)
        self.models_panel = self.drawer.models_panel
        self.approval_drawer = ApprovalDrawer(self)
        self.approval_drawer.hide()
        self.approval_drawer.approve_btn.clicked.connect(
            lambda: asyncio.create_task(self.ws_client.send_approval(True, self.approval_drawer._approval_key, override_class=self.approval_drawer.class_dropdown.currentText()))
        )
        self.approval_drawer.reject_btn.clicked.connect(
            lambda: asyncio.create_task(self.ws_client.send_approval(False, self.approval_drawer._approval_key))
        )

        self.drawer.opened.connect(self._on_drawer_opened)

        # Connect Cloud Provider events
        self.models_panel.validate_requested.connect(
            lambda p, k, u="": asyncio.create_task(self.ws_client.validate_provider(p, k, base_url=u))
        )
        self.models_panel.save_requested.connect(
            lambda p, k, d, m, u="": asyncio.create_task(self.ws_client.save_provider(p, k, d, m, base_url=u))
        )
        self.models_panel.remove_requested.connect(
            lambda p: asyncio.create_task(self.ws_client.remove_provider(p))
        )
        self.models_panel.refresh_models_requested.connect(
            lambda p: asyncio.create_task(self.ws_client.refresh_models(p))
        )

        # Connect Local Models & Task Download events
        self.models_panel.download_local_model_requested.connect(
            lambda m, d: asyncio.create_task(self.ws_client.start_local_model_download(m, d))
        )
        self.models_panel.delete_local_model_requested.connect(
            lambda m: asyncio.create_task(self.ws_client.delete_local_model(m))
        )

        # Connect Capabilities Check Scheduler events
        self.models_panel.set_capability_schedule_requested.connect(
            lambda s, m: asyncio.create_task(self.ws_client.set_capability_schedule(s, m))
        )
        self.models_panel.run_capability_now_requested.connect(
            lambda m: asyncio.create_task(self.ws_client.run_capability_check_now(m))
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0D1219"))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "drawer"):
            self.drawer.update_geometry()
        self._save_window_geometry()

    def _save_window_geometry(self):
        """Persist current window dimensions to ~/.aether/config.json."""
        if not self.isMinimized() and self.width() >= 380 and self.height() >= _MIN_WINDOW_H:
            self.user_config.window_width = self.width()
            self.user_config.window_height = self.height()
            save_config(self.user_config)

    def _toggle_settings(self):
        self.drawer.toggle()

    def _on_drawer_opened(self):
        asyncio.create_task(self.ws_client.request_provider_list())
        asyncio.create_task(self.ws_client.request_local_models_list())
        asyncio.create_task(self.ws_client.request_capability_history())

    def _on_ws_event(self, event: Event):
        """Dispatch incoming WebSocket events to HUD and Models panel."""
        self.hud.log_event(event)

        p = event.payload or {}
        t = event.type

        if t in (EventType.PROVIDER_LIST_RESPONSE, EventType.REFRESH_MODELS_RESPONSE):
            providers = p.get("providers", [])
            default_provider = p.get("default_provider", "google_gemini")
            default_model = p.get("default_model", "")
            self._current_default_provider = default_provider

            self.models_panel.populate_providers(providers)

            # Reset refreshing states
            for row in self.models_panel._provider_rows.values():
                row.detail.set_refreshing(False)

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
                self.hud.set_active_model(self._current_default_provider, target_model)

        elif t == EventType.LOCAL_MODEL_LIST_RESPONSE:
            self.models_panel.populate_local_models(p.get("models", []))

        elif t == EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS:
            m_name = p.get("model", "")
            pct = p.get("download_percent", 0.0)
            self.models_panel.update_download_progress(m_name, pct)

        elif t == EventType.LOCAL_MODEL_DELETE_RESPONSE:
            asyncio.create_task(self.ws_client.request_local_models_list())

        elif t == EventType.CAPABILITY_CHECK_HISTORY_RESPONSE:
            history = p.get("history", [])
            summary = p.get("summary")
            self.models_panel.cap_row.update_history(history, summary)

        elif t in (EventType.PROVIDER_SAVE_RESPONSE, EventType.PROVIDER_REMOVE_RESPONSE):
            asyncio.create_task(self.ws_client.request_provider_list())

        elif t in (EventType.PROVIDER_VALIDATE_RESPONSE, EventType.SETTINGS_PROVIDER_VALIDATE_RESULT):
            provider_name = p.get("provider", "")
            models = p.get("models", [])
            if provider_name in self.models_panel._provider_rows:
                row = self.models_panel._provider_rows[provider_name]
                status = p.get("status", "no_key")
                row.set_badge(status)
                if models:
                    row.update_models(models)
                    if row.detail.chk_default.isChecked() or getattr(row, "is_default", False):
                        self.hud.set_available_models(models)
            elif models:
                self.hud.set_available_models(models)

        elif t == EventType.TOOL_APPROVAL_REQUEST:
            self.approval_drawer.show_for_request(p)
            self.approval_drawer.setFocus()

        elif t in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED, EventType.WORKER_EXECUTION_COMPLETED):
            self.approval_drawer.hide_drawer()

    def _minimize_window(self):
        self._drag = QPoint()
        QApplication.restoreOverrideCursor()
        self.showMinimized()

    # Frameless drag
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= 40:
            pt = e.position().toPoint()
            child = self.childAt(pt)
            if child is None or child not in (
                getattr(self, "btn_gear", None),
                getattr(self, "btn_min", None),
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
                info.ptMinTrackSize.x = int(420 * dpr)
                info.ptMinTrackSize.y = int(_MIN_WINDOW_H * dpr)
                info.ptMaxTrackSize.x = int(_MAX_WINDOW_W * dpr)
                info.ptMaxTrackSize.y = int(_MAX_WINDOW_H * dpr)
                return True, 0

            if msg.message == 0x0084:  # WM_NCHITTEST
                # Use Qt's DPI-aware logical global cursor position
                pt = self.mapFromGlobal(self.cursor().pos())
                w, h = self.width(), self.height()

                # If cursor is outside window bounds or over interactive buttons/drawers, return HTCLIENT
                if pt.x() < 0 or pt.x() > w or pt.y() < 0 or pt.y() > h:
                    return super().nativeEvent(eventType, message)

                child = self.childAt(pt)
                if child is not None:
                    if (
                        child in (getattr(self, "btn_gear", None), getattr(self, "btn_min", None), getattr(self, "btn_close", None))
                        or (hasattr(self, "drawer") and self.drawer.isVisible() and self.drawer.rect().contains(pt))
                    ):
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
        asyncio.create_task(self.ws_client.disconnect())
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
