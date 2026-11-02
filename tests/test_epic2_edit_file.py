"""Epic 2, E2-03 + E2-04: edit_file, create_directory, write parents.

The edit contract: unique anchors, read-hash verification,
all-or-nothing application, atomic write — identical in the engine's
in-process implementation and the worker's (both ride aether_common).
"""

from __future__ import annotations

import hashlib

import pytest

from aether_common.text_edit import TextEditError, apply_text_edits, make_unified_diff
from aether_engine.tools.registry import ToolExecutionError, build_tool_registry
from aether_engine.validation.pre_flight import validate_tool_request
from aether_worker.__main__ import run_worker


def _hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The shared edit contract (aether_common.text_edit)
# ---------------------------------------------------------------------------

def test_apply_single_and_multiple_edits():
    text = "one\ntwo\nthree\n"
    out = apply_text_edits(text, [
        {"old_text": "two", "new_text": "TWO"},
        {"old_text": "three", "new_text": "THREE"},
    ])
    assert out == "one\nTWO\nTHREE\n"


def test_missing_anchor_fails_loudly():
    with pytest.raises(TextEditError, match="not found"):
        apply_text_edits("abc", [{"old_text": "zzz", "new_text": "q"}])


def test_ambiguous_anchor_fails_loudly():
    with pytest.raises(TextEditError, match="occurs 2 times"):
        apply_text_edits("x x", [{"old_text": "x", "new_text": "y"}])


def test_one_bad_edit_applies_nothing():
    with pytest.raises(TextEditError):
        apply_text_edits("alpha beta", [
            {"old_text": "alpha", "new_text": "ALPHA"},
            {"old_text": "missing", "new_text": "q"},
        ])


def test_diff_is_unified_and_bounded():
    diff = make_unified_diff("a\nb\n", "a\nB\n", "f.txt")
    assert "-b" in diff and "+B" in diff


# ---------------------------------------------------------------------------
# Registry tool (in-process implementation)
# ---------------------------------------------------------------------------

@pytest.fixture()
def registry(monkeypatch):
    """Registry whose worker dispatch runs the REAL worker logic in-process.

    The subprocess sandbox needs Windows Job Objects (pywin32), absent
    on this Linux VM; dispatching to ``run_worker`` directly exercises
    the identical code path the subprocess runs, minus isolation.
    """
    from aether_engine.workers import sandbox

    async def _in_process_worker(tool_name, args, task_class="standard", audit_logger=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return build_tool_registry()


@pytest.mark.asyncio
async def test_registry_edit_round_trip(tmp_path, registry):
    target = tmp_path / "ws" / "code.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = await registry.execute(
        "edit_file",
        {"path": str(target), "expected_hash": _hash(target),
         "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
        workspace_roots=[str(tmp_path / "ws")],
        policies={},
        approved=True,
    )
    assert result["edited"] is True and result["edits_applied"] == 1
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"


@pytest.mark.asyncio
async def test_registry_edit_stale_hash_refused(tmp_path, registry):
    target = tmp_path / "ws" / "code.py"
    target.parent.mkdir(parents=True)
    target.write_text("v1\n", encoding="utf-8")
    stale = _hash(target)
    target.write_text("v2 changed underneath\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="changed since it was read"):
        await registry.execute(
            "edit_file",
            {"path": str(target), "expected_hash": stale,
             "edits": [{"old_text": "v2 changed underneath", "new_text": "v3"}]},
            workspace_roots=[str(tmp_path / "ws")],
            policies={},
            approved=True,
        )
    assert target.read_text(encoding="utf-8") == "v2 changed underneath\n"


@pytest.mark.asyncio
async def test_registry_edit_outside_roots_denied_by_preflight(tmp_path, registry):
    target = tmp_path / "elsewhere.py"
    target.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="outside permitted workspace roots"):
        await registry.execute(
            "edit_file",
            {"path": str(target), "edits": [{"old_text": "1", "new_text": "2"}]},
            workspace_roots=[str(tmp_path / "ws")],
            policies={},
            approved=True,
        )


# ---------------------------------------------------------------------------
# Worker implementation (the graph path's real executor)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_edit_and_stale_hash(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    out = await run_worker({"tool_name": "edit_file", "args": {
        "path": str(target), "expected_hash": _hash(target),
        "edits": [{"old_text": "return 1", "new_text": "return 2"}],
    }})
    assert out["edited"] is True
    assert target.read_text(encoding="utf-8") == "return 2\n"

    with pytest.raises(ValueError, match="changed since it was read"):
        await run_worker({"tool_name": "edit_file", "args": {
            "path": str(target), "expected_hash": "deadbeef" * 8,
            "edits": [{"old_text": "return 2", "new_text": "return 3"}],
        }})
    assert target.read_text(encoding="utf-8") == "return 2\n"


@pytest.mark.asyncio
async def test_worker_create_directory_and_write_parents(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    out = await run_worker({"tool_name": "create_directory", "args": {"path": str(nested)}})
    assert out["created"] is True and nested.is_dir()

    deep_file = tmp_path / "x" / "y" / "f.txt"
    await run_worker({"tool_name": "write_file", "args": {"path": str(deep_file), "content": "hi"}})
    assert deep_file.read_text(encoding="utf-8") == "hi"


# ---------------------------------------------------------------------------
# Pre-flight + approval preview
# ---------------------------------------------------------------------------

def test_preflight_edit_and_create_directory(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.txt"
    f.write_text("hi\n", encoding="utf-8")
    roots = [str(ws)]
    assert validate_tool_request("edit_file", {"path": str(f)}, workspace_roots=roots).is_valid
    assert not validate_tool_request("edit_file", {"path": str(tmp_path / "o.txt")}, workspace_roots=roots).is_valid
    assert validate_tool_request("create_directory", {"path": str(ws / "new")}, workspace_roots=roots).is_valid
    assert not validate_tool_request("create_directory", {"path": str(tmp_path / "new")}, workspace_roots=roots).is_valid
    # write_file no longer fails just because parents are missing (D3).
    assert validate_tool_request("write_file", {"path": str(ws / "deep" / "f.txt")}, workspace_roots=roots).is_valid


def test_pause_preview_contains_diff(tmp_path):
    from aether_engine.langgraph.nodes.pause import _edit_preview

    target = tmp_path / "code.py"
    target.write_text("return 1\n", encoding="utf-8")
    preview = _edit_preview(
        {"path": str(target), "expected_hash": _hash(target),
         "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
        [str(tmp_path)],
    )
    assert "-return 1" in preview["diff"] and "+return 2" in preview["diff"]

    stale = _edit_preview(
        {"path": str(target), "expected_hash": "0" * 64,
         "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
        [str(tmp_path)],
    )
    assert "changed since the agent read it" in stale["error"]
