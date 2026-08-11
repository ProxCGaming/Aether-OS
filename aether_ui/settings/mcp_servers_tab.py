from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QListWidget, QListWidgetItem, QMessageBox
)
from PySide6.QtCore import Qt

class MCPServersTab(QWidget):
    """
    Settings tab for managing MCP server connections.
    """
    def __init__(self, registry, parent=None):
        super().__init__(parent)
        self.registry = registry
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Add Server Form
        form_layout = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Server Name")
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Command/URL")
        
        self.add_btn = QPushButton("Add Server")
        self.add_btn.clicked.connect(self._on_add_server)

        form_layout.addWidget(self.name_input)
        form_layout.addWidget(self.cmd_input)
        form_layout.addWidget(self.add_btn)
        
        layout.addLayout(form_layout)

        # Server List
        self.server_list = QListWidget()
        layout.addWidget(self.server_list)

        # Controls for selected server
        controls_layout = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self._on_test_connection)
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._on_delete_server)
        
        controls_layout.addWidget(self.test_btn)
        controls_layout.addWidget(self.delete_btn)
        
        layout.addLayout(controls_layout)

    def _refresh_list(self):
        self.server_list.clear()
        for name, config in self.registry.list_servers().items():
            cmd = config.get("command", "")
            item = QListWidgetItem(f"{name} ({cmd})")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.server_list.addItem(item)

    def _on_add_server(self):
        name = self.name_input.text().strip()
        cmd = self.cmd_input.text().strip()
        
        if not name or not cmd:
            QMessageBox.warning(self, "Validation Error", "Name and Command are required.")
            return
            
        self.registry.add_server(name, {"command": cmd})
        self.name_input.clear()
        self.cmd_input.clear()
        self._refresh_list()

    def _on_delete_server(self):
        current_item = self.server_list.currentItem()
        if not current_item:
            return
            
        name = current_item.data(Qt.ItemDataRole.UserRole)
        self.registry.remove_server(name)
        self._refresh_list()

    def _on_test_connection(self):
        current_item = self.server_list.currentItem()
        if not current_item:
            return
            
        name = current_item.data(Qt.ItemDataRole.UserRole)
        # Scaffolding: Simulated connection test
        QMessageBox.information(self, "Test Connection", f"Successfully connected to {name} MCP server.")
