from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame
)
import datetime

from aether_ui.theme import (
    SURFACE_PANEL,
    SURFACE_BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    BRAND_FRONTEND,
    SCROLLBAR_CSS
)

class SessionItem(QFrame):
    clicked = Signal(str)
    
    def __init__(self, session_id: str, title: str, timestamp: int, active: bool = False, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.title = title
        self.timestamp = timestamp
        self.active = active
        self._build_ui()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
    def _build_ui(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(10, 8, 10, 8)
        
        lbl_title = QLabel(self.title)
        lbl_title.setStyleSheet(f"color: {TEXT_PRIMARY if self.active else TEXT_SECONDARY}; font-weight: {'bold' if self.active else 'normal'}; font-size: 12px;")
        
        date_str = datetime.datetime.fromtimestamp(self.timestamp).strftime("%b %d, %H:%M") if self.timestamp else ""
        lbl_date = QLabel(date_str)
        lbl_date.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 10px;")
        
        lo.addWidget(lbl_title)
        lo.addWidget(lbl_date)
        
        bg = "#2A2D35" if self.active else "transparent"
        border = f"1px solid {BRAND_FRONTEND}" if self.active else "1px solid transparent"
        
        self.setStyleSheet(f"""
            SessionItem {{
                background-color: {bg};
                border: {border};
                border-radius: 6px;
            }}
            SessionItem:hover {{
                background-color: #2A2D35;
            }}
        """)
        
    def mousePressEvent(self, event):
        self.clicked.emit(self.session_id)
        super().mousePressEvent(event)


class SessionsDrawer(QWidget):
    session_selected = Signal(str)
    new_chat_requested = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sessions = []
        self.active_session_id = None
        self._build_ui()
        
    def _build_ui(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(10)
        
        # New Chat Button
        self.btn_new = QPushButton("+ New Chat")
        self.btn_new.setFixedHeight(36)
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.setStyleSheet(f"""
            QPushButton {{
                background-color: {BRAND_FRONTEND};
                color: #07150E;
                font-weight: bold;
                border-radius: 6px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: #38E5AC;
            }}
        """)
        self.btn_new.clicked.connect(self.new_chat_requested.emit)
        lo.addWidget(self.btn_new)
        
        # Scroll Area for sessions
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameStyle(0)
        self.scroll.setStyleSheet(SCROLLBAR_CSS + "QScrollArea { background: transparent; }")
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 10, 0)
        self.scroll_layout.setSpacing(5)
        self.scroll_layout.addStretch()
        
        self.scroll.setWidget(self.scroll_content)
        lo.addWidget(self.scroll)
        
    def set_sessions(self, sessions: list, active_id: str = None):
        self.sessions = sessions
        self.active_session_id = active_id
        self._refresh_list()
        
    def _refresh_list(self):
        # Clear existing items except stretch
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        # Add sessions
        for s in self.sessions:
            item = SessionItem(
                session_id=s.get("id"),
                title=s.get("title", "Chat"),
                timestamp=s.get("updated_at", 0),
                active=(s.get("id") == self.active_session_id)
            )
            item.clicked.connect(self.session_selected.emit)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, item)
