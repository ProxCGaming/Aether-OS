import json
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QHBoxLayout,
    QPushButton, QMessageBox
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from aether_ui.theme import (
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, SURFACE_BG, SURFACE_PANEL, SURFACE_BORDER,
    BRAND_FRONTEND
)


class AgentsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.net_mgr = QNetworkAccessManager(self)
        self.engine_url = "http://127.0.0.1:8000"
        self._build_ui()
        self._load_nodes()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # Header
        hdr_layout = QHBoxLayout()
        header = QLabel("Agents")
        header.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {TEXT_PRIMARY};")
        hdr_layout.addWidget(header)
        
        hdr_layout.addStretch()
        
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {SURFACE_PANEL};
                color: {TEXT_PRIMARY};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 6px;
                padding: 6px 12px;
            }}
            QPushButton:hover {{
                background: {SURFACE_BORDER};
            }}
        """)
        refresh_btn.clicked.connect(self._load_nodes)
        hdr_layout.addWidget(refresh_btn)
        
        main_layout.addLayout(hdr_layout)

        desc = QLabel("Live routing assignments for LangGraph nodes via CapabilityRouter.")
        desc.setStyleSheet(f"font-size: 14px; color: {TEXT_SECONDARY};")
        main_layout.addWidget(desc)

        # Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: transparent; }}")
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(12)
        
        scroll.setWidget(self.scroll_content)
        main_layout.addWidget(scroll)

    def _clear_layout(self):
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _load_nodes(self):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/agents/nodes"))
        reply = self.net_mgr.get(req)
        reply.finished.connect(lambda: self._on_load_finished(reply))

    def _on_load_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._clear_layout()
            err_lbl = QLabel(f"Failed to load nodes: {reply.errorString()}")
            err_lbl.setStyleSheet(f"color: {TEXT_MUTED};")
            self.scroll_layout.addWidget(err_lbl)
            self.scroll_layout.addStretch()
            reply.deleteLater()
            return
            
        data = json.loads(reply.readAll().data().decode())
        self._clear_layout()
        
        if not data:
            empty = QLabel("No agent nodes found.")
            empty.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 14px;")
            self.scroll_layout.addWidget(empty)
        else:
            for node in data:
                self._add_node_card(node)
                
        self.scroll_layout.addStretch()
        reply.deleteLater()

    def _add_node_card(self, node: dict):
        name = node.get("name", "unknown")
        model = node.get("model", "unknown")
        enabled = node.get("enabled", True)
        
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
            }}
        """)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        
        info_layout = QVBoxLayout()
        name_lbl = QLabel(name.capitalize())
        name_lbl.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        info_layout.addWidget(name_lbl)
        
        model_lbl = QLabel(f"Model: {model}")
        model_lbl.setStyleSheet(f"font-size: 13px; color: {TEXT_SECONDARY};")
        info_layout.addWidget(model_lbl)
        
        layout.addLayout(info_layout)
        layout.addStretch()
        
        toggle_btn = QPushButton("Disable" if enabled else "Enable")
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.clicked.connect(lambda: self._toggle_node(name, not enabled))
        layout.addWidget(toggle_btn)
        
        self.scroll_layout.addWidget(card)
        
    def _toggle_node(self, name: str, enabled: bool):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/agents/nodes/{name}/toggle"))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = json.dumps({"enabled": enabled}).encode()
        reply = self.net_mgr.sendCustomRequest(req, b"PATCH", payload)
        reply.finished.connect(lambda: self._on_action_finished(reply))
        
    def _on_action_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            QMessageBox.critical(self, "Error", f"Action failed: {reply.readAll().data().decode()}")
        else:
            self._load_nodes()
        reply.deleteLater()
