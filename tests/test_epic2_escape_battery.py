"""Epic 2, E2-12: the escape battery.

Every path-taking tool, old and new, faces the same battery of
escape attempts through the REAL registry pipeline — traversal,
absolute paths outside the roots, symlink escapes, sibling-prefix
confusion. Every attempt must be denied, and validation denials must
land in the audit trail.
"""

from __future__ import annotations

import pytest

from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

READ_TOOLS = ["read_file", "find_files", "search_text", "project_tree"]
WRITE_TOOLS = ["write_file", "edit_file", "create_directory", "delete_file", "run_checks"]


class _AuditStub:
    def __init__(self):
        self.events = []

    def log_event(self, action, details):
        self.events.append((action, details))


@pytest.fixture()
def scene(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    sibling = tmp_path / "ws2"
    sibling.mkdir()
    (sibling / "sib.txt").write_text("sibling\n", encoding="utf-8")
    link = ws / "escape_link.txt"
    link.symlink_to(outside)
    link_dir = ws / "escape_dir"
    link_dir.symlink_to(tmp_path)
    return {"ws": ws, "outside": outside, "sibling": sibling, "link": link, "link_dir": link_dir}


def _args_for(tool: str, target: str) -> dict:
    if tool == "read_file":
        return {"path": target}
    if tool == "find_files":
        return {"pattern": "*.txt", "path": target}
    if tool == "search_text":
        return {"query": "secret", "path": target}
    if tool in ("project_tree", "run_checks", "create_directory", "delete_file"):
        return {"path": target}
    if tool == "write_file":
        return {"path": target, "content": "pwned"}
    if tool == "edit_file":
        return {"path": target, "edits": [{"old_text": "secret", "new_text": "pwned"}]}
    raise AssertionError(tool)


def _targets(scene) -> dict[str, str]:
    ws = scene["ws"]
    return {
        "traversal": str(ws / ".." / "outside.txt"),
        "absolute_outside": str(scene["outside"]),
        "symlink_file": str(scene["link"]),
        "symlink_dir": str(scene["link_dir"] / "outside.txt"),
        "sibling_prefix": str(scene["sibling"] / "sib.txt"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", READ_TOOLS + WRITE_TOOLS)
async def test_every_escape_is_denied(scene, tool):
    registry = build_tool_registry()
    audit = _AuditStub()
    for name, target in _targets(scene).items():
        with pytest.raises(ToolExecutionError):
            await registry.execute(
                tool,
                _args_for(tool, target),
                workspace_roots=[str(scene["ws"])],
                policies={},
                approved=True,
                audit_logger=audit,
            )
        # The outside world is untouched.
        assert scene["outside"].read_text(encoding="utf-8") == "secret\n", (tool, name)


@pytest.mark.asyncio
async def test_denials_are_audit_logged(scene):
    registry = build_tool_registry()
    audit = _AuditStub()
    with pytest.raises(ToolExecutionError):
        await registry.execute(
            "read_file",
            {"path": str(scene["outside"])},
            workspace_roots=[str(scene["ws"])],
            policies={},
            audit_logger=audit,
        )
    actions = [a for a, _ in audit.events]
    assert "TOOL_VALIDATION_FAILED" in actions


@pytest.mark.asyncio
async def test_legitimate_paths_still_work(scene):
    registry = build_tool_registry()
    result = await registry.execute(
        "read_file",
        {"path": str(scene["ws"] / "inside.txt")},
        workspace_roots=[str(scene["ws"])],
        policies={},
    )
    assert "inside" in result
