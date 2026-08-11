import os
from pathlib import Path
from aether_engine.validation.pre_flight import validate_tool_request

def test_workspace_containment(tmp_path):
    root1 = tmp_path / "workspace1"
    root1.mkdir()
    
    in_file = root1 / "test.txt"
    res = validate_tool_request("write_file", {"path": str(in_file)}, workspace_roots=[str(root1)])
    assert res.is_valid

    out_file = tmp_path / "other" / "test.txt"
    res = validate_tool_request("write_file", {"path": str(out_file)}, workspace_roots=[str(root1)])
    assert not res.is_valid
    assert "outside permitted workspace roots" in res.error

    traversal_file = root1 / ".." / "test.txt"
    res = validate_tool_request("write_file", {"path": str(traversal_file)}, workspace_roots=[str(root1)])
    assert not res.is_valid
    
    traversal_in = root1 / "subdir" / ".." / "test.txt"
    res = validate_tool_request("write_file", {"path": str(traversal_in)}, workspace_roots=[str(root1)])
    assert res.is_valid
