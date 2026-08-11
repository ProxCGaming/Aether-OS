from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QRect, Qt
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QLabel, QWidget


class ApprovalDrawer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background:#0D1117; border:1px solid #30363D; border-radius:10px;")
        self._approval_key = None
        self._build_ui()
        self._animation = QPropertyAnimation(self, b"geometry")
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hidden_rect = None

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.title = QLabel("Tool Approval Required")
        self.title.setStyleSheet("font-weight:700; color:#F0F6FC;")
        layout.addWidget(self.title)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setStyleSheet("color:#C9D1D9; font-size:12px;")
        layout.addWidget(self.details)

        buttons = QVBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.reject_btn = QPushButton("Reject")
        self.approve_btn.setStyleSheet("background:#238636; color:white; border:none; border-radius:6px; padding:6px;")
        self.reject_btn.setStyleSheet("background:#DA3633; color:white; border:none; border-radius:6px; padding:6px;")
        buttons.addWidget(self.approve_btn)
        buttons.addWidget(self.reject_btn)
        layout.addLayout(buttons)

    def show_for_request(self, payload: dict):
        # This now handles LangGraph native pause events
        self._approval_key = payload.get("approval_key")
        tool_name = payload.get("tool_name", "tool")
        args = payload.get("args", {})
        node_origin = payload.get("node", "unknown")
        task_class = payload.get("task_class", "default")
        workspace = payload.get("workspace", "default")
        
        details_text = (
            f"Node: {node_origin}\n"
            f"Tool: {tool_name}\n"
            f"Task-Class: {task_class}\n"
            f"Workspace: {workspace}\n"
            f"Args: {args}"
        )
        self.details.setText(details_text)
        self._show()

    def _show(self):
        parent = self.parentWidget()
        if not parent:
            return
        rect = parent.rect()
        target = QRect(rect.width() - 280, 40, 240, 180)
        self.setGeometry(target)
        self.raise_()
        self.show()
        self._animation.stop()
        self._animation.setStartValue(QRect(target.x(), target.y() + 20, target.width(), target.height()))
        self._animation.setEndValue(target)
        self._animation.start()

    def hide_drawer(self):
        self._animation.stop()
        self._animation.setStartValue(self.geometry())
        self._animation.setEndValue(QRect(self.geometry().x(), self.geometry().y() + 20, self.geometry().width(), self.geometry().height()))
        self._animation.start()
        self._animation.finished.connect(self._final_hide, Qt.ConnectionType.UniqueConnection)

    def _final_hide(self):
        self.hide()
