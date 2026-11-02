"""Epic 2, E2-01: read_file — windows, content hash, containment.

The hash is the contract E2-03's edit_file consumes: it must describe
the exact bytes on disk and change when they change. Containment must
match pre-flight's rule (resolve, then inside-a-root) so the pipeline
and the tool never disagree.
"""

from __future__ import annotations

import hashlib

import pytest

from aether_engine.tools.filesystem import _read_file_fn
from aether_engine.tools.paths import PathContainmentError
from aether_engine.tools.registry import build_tool_registry
from aether_engine.validation.pre_flight import validate_tool_request


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "hello.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return root


def test_reads_with_line_numbers_and_hash_header(workspace):
    out = _read_file_fn(str(workspace / "hello.txt"), workspace_roots=[str(workspace)])
    expected = hashlib.sha256(b"alpha\nbeta\ngamma\n").hexdigest()
    assert f"sha256:{expected}" in out
    assert "lines:3" in out
    assert "1: alpha" in out and "3: gamma" in out


def test_window_read(workspace):
    big = workspace / "big.txt"
    big.write_text("".join(f"line {i}\n" for i in range(1, 51)), encoding="utf-8")
    out = _read_file_fn(str(big), offset=10, limit=5, workspace_roots=[str(workspace)])
    assert "showing 10-14" in out
    assert "10: line 10" in out and "14: line 14" in out
    assert "15: line 15" not in out


def test_hash_changes_when_file_changes(workspace):
    target = workspace / "hello.txt"
    first = _read_file_fn(str(target), workspace_roots=[str(workspace)])
    target.write_text("alpha\nCHANGED\ngamma\n", encoding="utf-8")
    second = _read_file_fn(str(target), workspace_roots=[str(workspace)])
    assert first.split("sha256:")[1].split(" ")[0] != second.split("sha256:")[1].split(" ")[0]


def test_missing_and_directory_and_binary(workspace):
    roots = [str(workspace)]
    assert "does not exist" in _read_file_fn(str(workspace / "nope.txt"), workspace_roots=roots)
    assert "not a file" in _read_file_fn(str(workspace), workspace_roots=roots)
    blob = workspace / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02binary")
    assert "binary" in _read_file_fn(str(blob), workspace_roots=roots)


def test_truncation_is_loud(workspace):
    big = workspace / "wide.txt"
    big.write_text("x" * 30000 + "\n", encoding="utf-8")
    out = _read_file_fn(str(big), workspace_roots=[str(workspace)])
    assert "[truncated at" in out


def test_escape_attempts_raise(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    roots = [str(workspace)]
    with pytest.raises(PathContainmentError):
        _read_file_fn(str(outside), workspace_roots=roots)
    with pytest.raises(PathContainmentError):
        _read_file_fn(str(workspace / ".." / "outside.txt"), workspace_roots=roots)
    # Sibling-prefix confusion: ws2 is not inside ws.
    sibling = tmp_path / "ws2"
    sibling.mkdir()
    (sibling / "s.txt").write_text("s", encoding="utf-8")
    with pytest.raises(PathContainmentError):
        _read_file_fn(str(sibling / "s.txt"), workspace_roots=roots)


def test_symlink_escape_raises(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(PathContainmentError):
        _read_file_fn(str(link), workspace_roots=[str(workspace)])


def test_preflight_agrees_with_tool(workspace, tmp_path):
    inside = validate_tool_request("read_file", {"path": str(workspace / "hello.txt")}, workspace_roots=[str(workspace)])
    assert inside.is_valid
    outside = validate_tool_request("read_file", {"path": str(tmp_path / "x.txt")}, workspace_roots=[str(workspace)])
    assert not outside.is_valid


@pytest.mark.asyncio
async def test_registry_execution_and_plan_mode_membership(workspace):
    registry = build_tool_registry()
    assert "read_file" in registry.read_only_names()
    result = await registry.execute(
        "read_file",
        {"path": str(workspace / "hello.txt")},
        workspace_roots=[str(workspace)],
        policies={},
    )
    assert "1: alpha" in result
