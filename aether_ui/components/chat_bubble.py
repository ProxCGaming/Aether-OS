from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QFrame
)
import markdown
import re

from aether_ui.theme import (
    SURFACE_PANEL,
    SURFACE_BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_MUTED,
    BRAND_ENGINE,
    BRAND_FRONTEND,
    SCROLLBAR_CSS
)

class ChatBubble(QWidget):
    def __init__(self, role: str, content: str, parent=None):
        super().__init__(parent)
        self.role = role
        self.raw_content = content
        self._build_ui()

    def _build_ui(self):
        lo = QVBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 10)
        
        container = QFrame()
        container.setObjectName("BubbleContainer")
        clo = QVBoxLayout(container)
        
        # Header (Role Name)
        header = QLabel("USER" if self.role == "user" else "AETHER")
        header.setStyleSheet(f"font-weight: bold; font-size: 11px; color: {BRAND_FRONTEND if self.role == 'user' else BRAND_ENGINE};")
        clo.addWidget(header)
        
        # Content
        self.content_view = QTextEdit()
        self.content_view.setReadOnly(True)
        self.content_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_view.setFrameStyle(0)
        self.content_view.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        
        html_content = self._format_content(self.raw_content)
        self.content_view.setHtml(html_content)
        
        if self.role == "user":
            bg = "#1A1D26"
            border = f"1px solid {BRAND_FRONTEND}40"
        else:
            bg = "#14171F"
            border = f"1px solid {SURFACE_BORDER}"
            
        container.setStyleSheet(f"""
            #BubbleContainer {{
                background-color: {bg};
                border: {border};
                border-radius: 8px;
            }}
            QTextEdit {{
                background-color: transparent;
                color: {TEXT_PRIMARY};
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
                line-height: 1.5;
            }}
        """)
        
        clo.addWidget(self.content_view)
        lo.addWidget(container)
        
    def _format_content(self, text: str) -> str:
        # Format <think> blocks as distinct stylized divs
        text = re.sub(
            r'<think>(.*?)</think>', 
            r'<div style="color: #8B949E; border-left: 2px solid #30363D; padding-left: 10px; margin-bottom: 10px; margin-top: 10px; font-style: italic; font-size: 11px;">🤔 Thinking...<br/>\1</div>', 
            text, 
            flags=re.DOTALL
        )
        text = re.sub(
            r'<think>(.*)$', 
            r'<div style="color: #8B949E; border-left: 2px solid #30363D; padding-left: 10px; margin-bottom: 10px; margin-top: 10px; font-style: italic; font-size: 11px;">🤔 Thinking...<br/>\1</div>', 
            text, 
            flags=re.DOTALL
        )
        
        html = markdown.markdown(text, extensions=['fenced_code', 'tables'])
        return html
        
    def _adjust_height(self):
        doc_height = self.content_view.document().size().height()
        self.content_view.setFixedHeight(int(doc_height) + 10)
