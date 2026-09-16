import json
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QHBoxLayout,
    QPushButton, QFileDialog, QMessageBox, QLineEdit
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from aether_ui.theme import (
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, SURFACE_BG, SURFACE_PANEL, SURFACE_BORDER,
    BRAND_FRONTEND, STATUS_OFFLINE
)


class PluginsTab(QWidget):
    def __init__(self, ws_client, parent=None):
        super().__init__(parent)
        self.ws_client = ws_client  # We might use WS or direct HTTP. We'll use QNetworkAccessManager for HTTP.
        self.net_mgr = QNetworkAccessManager(self)
        self.engine_url = "http://127.0.0.1:8000" # Assuming default engine URL
        self._build_ui()
        self._load_plugins()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # Header
        hdr_layout = QHBoxLayout()
        header = QLabel("Plugins")
        header.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {TEXT_PRIMARY};")
        hdr_layout.addWidget(header)
        
        hdr_layout.addStretch()
        
        install_btn = QPushButton("+ Install Plugin")
        install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        install_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {STATUS_OFFLINE};
                border: 1px solid {STATUS_OFFLINE};
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }}
        """)
        install_btn.clicked.connect(self._on_install_clicked)
        hdr_layout.addWidget(install_btn)
        
        main_layout.addLayout(hdr_layout)

        desc = QLabel("Plugins bundle skills, tools, and MCP servers into installable packages.")
        desc.setStyleSheet(f"font-size: 14px; color: {TEXT_SECONDARY};")
        desc.setWordWrap(True)
        main_layout.addWidget(desc)

        # Scroll Area for Plugins
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

    def _load_plugins(self):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/plugins"))
        reply = self.net_mgr.get(req)
        reply.finished.connect(lambda: self._on_load_finished(reply))

    def _on_load_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._clear_layout()
            err_lbl = QLabel(f"Failed to load plugins: {reply.errorString()}")
            err_lbl.setStyleSheet(f"color: {TEXT_MUTED};")
            self.scroll_layout.addWidget(err_lbl)
            self.scroll_layout.addStretch()
            reply.deleteLater()
            return
            
        data = json.loads(reply.readAll().data().decode())
        self._clear_layout()
        
        if not data:
            empty = QLabel("No plugins installed.")
            empty.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 14px;")
            self.scroll_layout.addWidget(empty)
        else:
            for name, p_data in data.items():
                self._add_plugin_card(name, p_data)
                
        self.scroll_layout.addStretch()
        reply.deleteLater()

    def _add_plugin_card(self, name: str, data: dict):
        manifest = data.get("manifest", {})
        enabled = data.get("enabled", True)
        
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        
        # Top Row
        top_row = QHBoxLayout()
        name_lbl = QLabel(f"{name} (v{manifest.get('version', '1.0')})")
        name_lbl.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        top_row.addWidget(name_lbl)
        
        top_row.addStretch()
        
        toggle_btn = QPushButton("Disable" if enabled else "Enable")
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.clicked.connect(lambda: self._toggle_plugin(name, not enabled))
        top_row.addWidget(toggle_btn)
        
        del_btn = QPushButton("Uninstall")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"color: {STATUS_OFFLINE};")
        del_btn.clicked.connect(lambda: self._uninstall_plugin(name))
        top_row.addWidget(del_btn)
        
        layout.addLayout(top_row)
        
        # Description
        desc_lbl = QLabel(manifest.get("description", ""))
        desc_lbl.setStyleSheet(f"font-size: 13px; color: {TEXT_SECONDARY};")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)
        
        self.scroll_layout.addWidget(card)

    def _on_install_clicked(self):
        # We can either ask for URL or File. Let's use File dialog for simplicity.
        path, _ = QFileDialog.getOpenFileName(self, "Select Plugin Zip or Directory", "", "Zip Files (*.zip);;All Files (*)")
        if not path:
            # Try asking for directory
            path = QFileDialog.getExistingDirectory(self, "Select Plugin Directory")
            if not path:
                return

        req = QNetworkRequest(QUrl(f"{self.engine_url}/plugins/install"))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = json.dumps({"source": path}).encode()
        
        reply = self.net_mgr.post(req, payload)
        reply.finished.connect(lambda: self._on_install_prepare_finished(reply))
        
    def _on_install_prepare_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            QMessageBox.critical(self, "Install Error", f"Failed to prepare plugin: {reply.readAll().data().decode()}")
            reply.deleteLater()
            return
            
        data = json.loads(reply.readAll().data().decode())
        reply.deleteLater()
        
        # Fire a synthetic tool approval request to the main window
        if self.ws_client and self.ws_client.callback:
            # We create a fake event payload for the ApprovalDrawer
            import uuid
            approval_key = str(uuid.uuid4())
            
            # Store the prepare_data so we can confirm it when approved
            self._pending_install_data = data
            self._pending_approval_key = approval_key
            
            fake_event = {
                "type": "TOOL_APPROVAL_REQUEST",
                "request_id": approval_key,
                "payload": {
                    "request_type": "plugin_install",
                    "approval_key": approval_key,
                    "manifest": data.get("manifest", {}),
                }
            }
            # Route to the callback which shows the drawer
            self.ws_client.callback(fake_event)
            
            # We also need to intercept the response from the drawer.
            # We'll hook into the main window's ws_client logic or do it directly.
            # Since the drawer sends a msg via ws_client, the backend will receive it.
            # BUT the backend doesn't know about plugin approvals through WS, it expects REST.
            # Actually, the ApprovalDrawer emits a signal or calls ws_client.send().
            # For this Phase 5.5, the prompt says "Reuse HITL". 
            # We'll intercept it via a quick patch to how we listen, or we can just confirm directly if the user clicks it.
            # Wait, the easiest way to reuse HITL without changing WS protocol too much:
            pass

    def handle_approval_result(self, approval_key: str, approved: bool):
        if hasattr(self, "_pending_approval_key") and self._pending_approval_key == approval_key:
            if approved:
                self._confirm_install(self._pending_install_data)
            self._pending_approval_key = None
            self._pending_install_data = None
            return True
        return False

    def _confirm_install(self, prepare_data: dict):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/plugins/confirm"))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = json.dumps({"prepare_data": prepare_data}).encode()
        
        reply = self.net_mgr.post(req, payload)
        reply.finished.connect(lambda: self._on_confirm_finished(reply))
        
    def _on_confirm_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            QMessageBox.critical(self, "Install Error", f"Failed to install plugin: {reply.readAll().data().decode()}")
        else:
            self._load_plugins()
        reply.deleteLater()

    def _uninstall_plugin(self, name: str):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/plugins/{name}"))
        reply = self.net_mgr.deleteResource(req)
        reply.finished.connect(lambda: self._on_action_finished(reply))
        
    def _toggle_plugin(self, name: str, enabled: bool):
        req = QNetworkRequest(QUrl(f"{self.engine_url}/plugins/{name}/toggle"))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = json.dumps({"enabled": enabled}).encode()
        reply = self.net_mgr.sendCustomRequest(req, b"PATCH", payload)
        reply.finished.connect(lambda: self._on_action_finished(reply))
        
    def _on_action_finished(self, reply: QNetworkReply):
        if reply.error() != QNetworkReply.NetworkError.NoError:
            QMessageBox.critical(self, "Error", f"Action failed: {reply.readAll().data().decode()}")
        else:
            self._load_plugins()
        reply.deleteLater()
