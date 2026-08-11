import pytest
from unittest.mock import AsyncMock, patch

def test_approval_drawer_override():
    try:
        from PySide6.QtWidgets import QApplication
        from aether_ui.components.approval_drawer import ApprovalDrawer
    except ImportError:
        pytest.skip("PySide6 not installed")

    app = QApplication.instance() or QApplication([])

    drawer = ApprovalDrawer()
    
    payload = {
        "tool_name": "execute_shell",
        "args": {},
        "approval_key": "test_key",
        "task_class": "standard",
        "workspace_roots": ["/some/path"]
    }
    
    # We monkeypatch the parent so show_for_request doesn't error out on None parent
    drawer.setParent(None)
    with patch.object(drawer, '_show'):
        drawer.show_for_request(payload)
        
        assert drawer.class_dropdown.currentText() == "standard"
        
        idx = drawer.class_dropdown.findText("heavy")
        drawer.class_dropdown.setCurrentIndex(idx)
        assert drawer.class_dropdown.currentText() == "heavy"
