"""Tool Approval Drawer / Modal matching Figma Page 11 Ionized Void styling."""
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from aether_ui.theme import (
    BRAND_ENGINE,
    COMBO_CSS,
    SCROLLBAR_CSS,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_OFFLINE,
    SURFACE_BG,
    SURFACE_BORDER,
    SURFACE_PANEL,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class ApprovalDrawer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet(f"""
            ApprovalDrawer {{
                background: {SURFACE_PANEL};
                border: 1px solid {STATUS_DEGRADED}80;
                border-radius: 10px;
            }}
        """)
        self._approval_key = None
        self._build_ui()
        self._animation = QPropertyAnimation(self, b"geometry")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # Header Row
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        dot = QLabel("●")
        dot.setStyleSheet(f"font-size:12px; color:{STATUS_DEGRADED};")
        hdr.addWidget(dot)

        self.title = QLabel("Tool Approval Required")
        self.title.setStyleSheet(f"font-weight:700; font-size:12px; color:{TEXT_PRIMARY}; border:none; background:transparent; font-family:'Segoe UI', sans-serif;")
        hdr.addWidget(self.title)
        hdr.addStretch()
        layout.addLayout(hdr)

        # Details in a compact scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background: {SURFACE_BG}; border: 1px solid {SURFACE_BORDER}; border-radius: 6px; }} {SCROLLBAR_CSS}")
        scroll.setMinimumHeight(55)
        scroll.setMaximumHeight(95)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setStyleSheet(f"color:{TEXT_SECONDARY}; font-family: Consolas, 'Cascadia Code', monospace; font-size:11px; padding:6px; background:transparent; border:none;")
        scroll.setWidget(self.details)
        layout.addWidget(scroll)

        # Task Class Row
        dropdown_label = QLabel("Task Class / Sandboxing Level:")
        dropdown_label.setStyleSheet(f"color:{TEXT_MUTED}; font-size:10px; font-weight:600; border:none; background:transparent;")
        layout.addWidget(dropdown_label)

        self.class_dropdown = QComboBox()
        self.class_dropdown.addItems(["standard", "high", "heavy", "quick", "instant", "custom"])
        self.class_dropdown.setMinimumHeight(28)
        self.class_dropdown.setStyleSheet(COMBO_CSS)
        layout.addWidget(self.class_dropdown)

        # Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.approve_btn = QPushButton("✓ Allow")
        self.approve_btn.setMinimumHeight(32)
        self.approve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.approve_btn.setStyleSheet(f"""
            QPushButton {{
                background: {STATUS_HEALTHY};
                color: #07150E;
                border: none;
                border-radius: 6px;
                font-weight: 700;
                font-size: 12px;
                font-family: 'Segoe UI', sans-serif;
                padding: 6px 12px;
            }}
            QPushButton:hover {{ background: #38E5AC; }}
        """)

        self.reject_btn = QPushButton("✕ Deny")
        self.reject_btn.setMinimumHeight(32)
        self.reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reject_btn.setStyleSheet(f"""
            QPushButton {{
                background: {STATUS_OFFLINE};
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                font-weight: 700;
                font-size: 12px;
                font-family: 'Segoe UI', sans-serif;
                padding: 6px 12px;
            }}
            QPushButton:hover {{ background: #FA5870; }}
        """)

        btn_layout.addWidget(self.approve_btn, 1)
        btn_layout.addWidget(self.reject_btn, 1)
        layout.addLayout(btn_layout)

    def get_selected_class(self) -> str:
        text = self.class_dropdown.currentText().lower()
        if text == "high":
            return "heavy"
        return text

    def show_for_request(self, payload: dict):
        self._approval_key = payload.get("approval_key")
        tool_name = payload.get("tool_name", "tool")
        args = payload.get("args", {})
        node_origin = payload.get("node", "unknown")
        task_class = payload.get("task_class", "standard")
        
        idx = self.class_dropdown.findText(task_class)
        if idx >= 0:
            self.class_dropdown.setCurrentIndex(idx)
            
        roots = payload.get("workspace_roots", [])
        roots_str = ", ".join(roots) if roots else payload.get("workspace", "None")
        
        details_text = (
            f"Tool: {tool_name}\n"
            f"Node: {node_origin}\n"
            f"Class: {task_class}\n"
            f"Workspace: {roots_str}\n"
            f"Args: {args}"
        )
        self.details.setText(details_text)
        self._show()

    def update_geometry(self):
        parent = self.parentWidget()
        if not parent or not self.isVisible():
            return
        rect = parent.rect()
        target_w = min(280, max(230, int(rect.width() * 0.55)))
        content_h = self.sizeHint().height()
        target_h = max(210, min(content_h, rect.height() - 50))
        target_x = max(10, rect.width() - target_w - 14)
        target = QRect(target_x, 46, target_w, target_h)
        self.setGeometry(target)
        self.raise_()

    def _show(self):
        try:
            self._animation.finished.disconnect(self._final_hide)
        except RuntimeError:
            pass
        parent = self.parentWidget()
        if not parent:
            return
        rect = parent.rect()
        target_w = min(280, max(230, int(rect.width() * 0.55)))
        content_h = self.sizeHint().height()
        target_h = max(210, min(content_h, rect.height() - 50))
        target_x = max(10, rect.width() - target_w - 14)
        target = QRect(target_x, 46, target_w, target_h)

        self.setGeometry(target)
        self.raise_()
        self.show()

        self._animation.stop()
        self._animation.setStartValue(QRect(target.x(), target.y() - 10, target.width(), target.height()))
        self._animation.setEndValue(target)
        self._animation.start()

    def hide_drawer(self):
        if not self.isVisible():
            return
        curr = self.geometry()
        self._animation.stop()
        self._animation.setStartValue(curr)
        self._animation.setEndValue(QRect(curr.x(), curr.y() - 10, curr.width(), curr.height()))
        self._animation.start()
        self._animation.finished.connect(self._final_hide, Qt.ConnectionType.UniqueConnection)

    def _final_hide(self):
        self.hide()
        try:
            self._animation.finished.disconnect(self._final_hide)
        except RuntimeError:
            pass
