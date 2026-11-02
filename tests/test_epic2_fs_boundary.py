"""E2-F01/F02/F03: filesystem tools honour the boundary exactly.

Three defects, one family — no truthiness shortcuts, no traversal
path that reaches outside the granted roots or into policy-denied
descendants:

- F01: a SUPPLIED-but-empty ``expected_hash`` must not skip
  edit_file's optimistic-concurrency check (absent/None still means
  "no hash", and the normal no-hash flow is unchanged).
- F02: project_tree must not descend into a directory symlink whose
  resolved target leaves the granted roots.
- F03: search_text must not open walk-discovered descendants that
  the sensitive floor or an explicit path-Deny rule excludes.
"""

from __future__ import annotations

import hashlib

import pytest

from aether_engine.langgraph.nodes.pause import _edit_preview
from aether_engine.tools.codebase import _project_tree_fn
from aether_engine.tools.filesystem import _search_text_fn
from aether_engine.tools.registry import (
    ToolExecutionError,
    build_tool_registry,
    create_file_tools,
)
from aether_worker.__main__ import run_worker


def _hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# E2-F01 — edit_file hash guard: supplied means supplied
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry(monkeypatch):
    """Registry whose worker dispatch runs the REAL worker logic in-process
    (same fixture pattern as test_epic2_edit_file.py)."""
    from aether_engine.workers import sandbox

    async def _in_process_worker(tool_name, args, task_class="standard", audit_logger=None, task_classes=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return build_tool_registry()


@pytest.mark.asyncio
async def test_worker_empty_hash_does_not_skip_the_check(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    _hash(target)  # the agent read the file at this state
    target.write_text("return 2 changed underneath\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since it was read"):
        await run_worker({"tool_name": "edit_file", "args": {
            "path": str(target), "expected_hash": "",
            "edits": [{"old_text": "return 2 changed underneath", "new_text": "return 3"}],
        }})
    assert target.read_text(encoding="utf-8") == "return 2 changed underneath\n"


@pytest.mark.asyncio
async def test_worker_missing_or_null_hash_keeps_normal_flow(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    out = await run_worker({"tool_name": "edit_file", "args": {
        "path": str(target),
        "edits": [{"old_text": "return 1", "new_text": "return 2"}],
    }})
    assert out["edited"] is True
    out = await run_worker({"tool_name": "edit_file", "args": {
        "path": str(target), "expected_hash": None,
        "edits": [{"old_text": "return 2", "new_text": "return 3"}],
    }})
    assert out["edited"] is True
    assert target.read_text(encoding="utf-8") == "return 3\n"


@pytest.mark.asyncio
async def test_registry_pipeline_empty_hash_refused(tmp_path, registry):
    target = tmp_path / "ws" / "code.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1\n", encoding="utf-8")
    target.write_text("v2 changed underneath\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="changed since it was read"):
        await registry.execute(
            "edit_file",
            {"path": str(target), "expected_hash": "",
             "edits": [{"old_text": "v2 changed underneath", "new_text": "v3"}]},
            workspace_roots=[str(tmp_path / "ws")],
            policies={},
            approved=True,
        )
    assert target.read_text(encoding="utf-8") == "v2 changed underneath\n"


def _inprocess_edit_fn():
    return {t.name: t for t in create_file_tools()}["edit_file"].execute_fn


def test_inprocess_empty_hash_refused(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    target.write_text("return 2 changed underneath\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="changed since it was read"):
        _inprocess_edit_fn()(
            path=str(target), expected_hash="",
            edits=[{"old_text": "return 2 changed underneath", "new_text": "return 3"}],
        )
    assert target.read_text(encoding="utf-8") == "return 2 changed underneath\n"


def test_inprocess_hash_controls_still_behave(tmp_path):
    edit = _inprocess_edit_fn()
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    # Correct current hash: edit proceeds.
    out = edit(path=str(target), expected_hash=_hash(target),
               edits=[{"old_text": "return 1", "new_text": "return 2"}])
    assert "Edited file" in out
    # Wrong hash: refused, file untouched.
    with pytest.raises(ToolExecutionError, match="changed since it was read"):
        edit(path=str(target), expected_hash="deadbeef" * 8,
             edits=[{"old_text": "return 2", "new_text": "return 3"}])
    assert target.read_text(encoding="utf-8") == "return 2\n"
    # No hash at all: the normal no-hash flow still edits.
    out = edit(path=str(target),
               edits=[{"old_text": "return 2", "new_text": "return 3"}])
    assert "Edited file" in out


def test_preview_empty_hash_warns_like_a_stale_hash(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    preview = _edit_preview(
        {"path": str(target), "expected_hash": "",
         "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
        [str(tmp_path)],
    )
    assert "changed since the agent read it" in preview["error"]
    # No hash supplied: the preview still produces a diff.
    preview = _edit_preview(
        {"path": str(target),
         "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
        [str(tmp_path)],
    )
    assert "-return 1" in preview["diff"] and "+return 2" in preview["diff"]


# ---------------------------------------------------------------------------
# E2-F02 — project_tree must not descend out of the granted roots
# ---------------------------------------------------------------------------


@pytest.fixture()
def tree_scene(tmp_path):
    ws = tmp_path / "ws"
    (ws / "real").mkdir(parents=True)
    (ws / "real" / "in.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "OUTSIDE_FILENAME_SENTINEL.txt").write_text("outside\n", encoding="utf-8")
    (ws / "linked_out").symlink_to(outside, target_is_directory=True)
    return ws


def test_project_tree_does_not_descend_into_escaping_symlink(tree_scene):
    out = _project_tree_fn(str(tree_scene), workspace_roots=[str(tree_scene)])
    assert "OUTSIDE_FILENAME_SENTINEL" not in out
    # The ordinary tree is intact.
    assert "real/" in out and "in.txt" in out


def test_project_tree_still_descends_within_the_roots(tree_scene, tmp_path):
    # A link to another directory inside the granted roots (but outside
    # the listed subtree) resolves inside the boundary — policy allows
    # it, exactly as read_file/search_text would.
    other = tmp_path / "ws" / "other"
    other.mkdir()
    (other / "other_file.txt").write_text("x\n", encoding="utf-8")
    (tree_scene / "real" / "alias").symlink_to(other, target_is_directory=True)
    out = _project_tree_fn(str(tree_scene / "real"), workspace_roots=[str(tree_scene)])
    assert "other_file.txt" in out


# ---------------------------------------------------------------------------
# E2-F03 — search_text must not open floor/denied descendants
# ---------------------------------------------------------------------------


@pytest.fixture()
def search_scene(tmp_path):
    ws = tmp_path / "ws"
    (ws / "denied").mkdir(parents=True)
    (ws / "open.txt").write_text("needle in the open\n", encoding="utf-8")
    (ws / "denied" / "note.txt").write_text("needle DENIED_SENTINEL\n", encoding="utf-8")
    return ws


def test_search_skips_path_denied_descendants(search_scene):
    rules = [{"prefix": str(search_scene / "denied"), "verdict": "Deny"}]
    out = _search_text_fn("needle", workspace_roots=[str(search_scene)], path_rules=rules)
    assert "open.txt:1: needle in the open" in out
    assert "DENIED_SENTINEL" not in out and "denied" not in out
    # Control: without the rule, the same search finds both files —
    # the filter is the rule, not the fixture.
    out = _search_text_fn("needle", workspace_roots=[str(search_scene)])
    assert "DENIED_SENTINEL" in out


def test_search_skips_denied_descendants_under_path_arg(search_scene):
    rules = [{"prefix": str(search_scene / "denied"), "verdict": "Deny"}]
    out = _search_text_fn(
        "needle", path=str(search_scene), workspace_roots=[str(search_scene)],
        path_rules=rules,
    )
    assert "DENIED_SENTINEL" not in out
    assert "open.txt:1: needle in the open" in out


@pytest.mark.asyncio
async def test_search_pipeline_applies_deny_rules_to_descendants(search_scene):
    registry = build_tool_registry()
    rules = [{"prefix": str(search_scene / "denied"), "verdict": "Deny"}]
    result = await registry.execute(
        "search_text",
        {"query": "needle"},
        workspace_roots=[str(search_scene)],
        policies={},
        path_rules=rules,
    )
    assert "open.txt:1: needle in the open" in result
    assert "DENIED_SENTINEL" not in result


def test_search_skips_sensitive_floor_descendants(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "key.txt").write_text("needle SSH_SENTINEL\n", encoding="utf-8")
    (home / "ws").mkdir()
    (home / "ws" / "ok.txt").write_text("needle in the open\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    # The broad-grant scenario from the finding: the whole home is a
    # granted root, so containment alone lets the walk reach .ssh.
    out = _search_text_fn("needle", include_hidden=True, workspace_roots=[str(home)])
    assert "ws/ok.txt:1: needle in the open" in out
    assert "SSH_SENTINEL" not in out and ".ssh" not in out


def test_search_single_file_target_is_the_judged_path(tmp_path, monkeypatch):
    # An explicit single-file target was judged by policy as THIS path
    # (the floor forces an approval for it; a Deny blocks the call
    # before execution). The tool does not second-guess that judgement
    # for the target itself — the per-descendant filter is for files
    # the walk DISCOVERS, which policy never saw.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "key.txt"
    key.write_text("needle SSH_SENTINEL\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    out = _search_text_fn("needle", path=str(key), workspace_roots=[str(home)])
    assert "SSH_SENTINEL" in out
