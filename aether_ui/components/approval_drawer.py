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


class ApprovalDrawer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("""
            ApprovalDrawer {
                background: #0F172A;
                border: 1px solid #334155;
                border-radius: 10px;
            }
        """)
        self._approval_key = None
        self._build_ui()
        self._animation = QPropertyAnimation(self, b"geometry")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Compact Header
        self.title = QLabel("🛡️ Tool Approval Required")
        self.title.setStyleSheet("font-weight:700; font-size:12px; color:#F8FAFC; border:none; background:transparent;")
        layout.addWidget(self.title)

        # Details in a compact scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: #020617; border: 1px solid #1E293B; border-radius: 6px; }")
        scroll.setMinimumHeight(48)
        scroll.setMaximumHeight(85)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setStyleSheet("color:#E2E8F0; font-family: Consolas, 'Courier New', monospace; font-size:10px; padding:4px; background:transparent; border:none;")
        scroll.setWidget(self.details)
        layout.addWidget(scroll)

        # Dropdown with Standard / High Settings
        dropdown_label = QLabel("Task Class / Settings:")
        dropdown_label.setStyleSheet("color:#94A3B8; font-size:10px; font-weight:600; border:none; background:transparent;")
        layout.addWidget(dropdown_label)

        self.class_dropdown = QComboBox()
        self.class_dropdown.addItems(["standard", "high", "heavy", "quick", "instant", "custom"])
        self.class_dropdown.setMinimumHeight(28)
        self.class_dropdown.setStyleSheet("""
            QComboBox {
                background: #1E293B;
                color: #F8FAFC;
                border: 1px solid #475569;
                border-radius: 5px;
                padding: 2px 6px;
                font-size: 11px;
                font-weight: 600;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #1E293B;
                color: #F8FAFC;
                selection-background-color: #3B82F6;
            }
        """)
        layout.addWidget(self.class_dropdown)

        # Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.approve_btn = QPushButton("✓ Approve")
        self.approve_btn.setMinimumHeight(30)
        self.approve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.approve_btn.setStyleSheet("""
            QPushButton {
                background: #10B981;
                color: #FFFFFF;
                border: none;
                border-radius: 5px;
                font-weight: 700;
                font-size: 12px;
                padding: 4px 8px;
            }
            QPushButton:hover { background: #059669; }
        """)

        self.reject_btn = QPushButton("✕ Reject")
        self.reject_btn.setMinimumHeight(30)
        self.reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reject_btn.setStyleSheet("""
            QPushButton {
                background: #EF4444;
                color: #FFFFFF;
                border: none;
                border-radius: 5px;
                font-weight: 700;
                font-size: 12px;
                padding: 4px 8px;
            }
            QPushButton:hover { background: #DC2626; }
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
        # This now handles LangGraph native pause events
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
            f"Node: {node_origin}\n"
            f"Tool: {tool_name}\n"
            f"Task-Class: {task_class}\n"
            f"Workspace: {roots_str}\n"
            f"Args: {args}"
        )
        self.details.setText(details_text)
        self._show()

    def update_geometry(self):
        """Keep compact drawer snapped to top-right corner of parent window without overlapping main controls."""
        parent = self.parentWidget()
        if not parent or not self.isVisible():
            return
        rect = parent.rect()
        target_w = min(250, max(210, int(rect.width() * 0.5)))
        content_h = self.sizeHint().height()
        target_h = max(200, min(content_h, rect.height() - 50))
        target_x = max(10, rect.width() - target_w - 12)
        target = QRect(target_x, 40, target_w, target_h)
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
        target_w = min(250, max(210, int(rect.width() * 0.5)))
        content_h = self.sizeHint().height()
        target_h = max(200, min(content_h, rect.height() - 50))
        target_x = max(10, rect.width() - target_w - 12)
        target = QRect(target_x, 40, target_w, target_h)

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
