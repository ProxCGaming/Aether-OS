"""Epic 2, E2-02: find_files + search_text — glob/search with containment.

Same contract as E2-01: the tools and pre-flight must agree on the
containment boundary, hidden material and .git stay out unless asked,
and every cap is loud — nothing truncates silently.
"""

from __future__ import annotations

import pytest

from aether_engine.tools import filesystem
from aether_engine.tools.filesystem import _find_files_fn, _search_text_fn
from aether_engine.tools.paths import PathContainmentError
from aether_engine.tools.registry import build_tool_registry
from aether_engine.validation.pre_flight import validate_tool_request


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "top.py").write_text("print('top')\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("import os\n\nNEEDLE = 1\n", encoding="utf-8")
    (root / "src" / "notes.txt").write_text("needle in text\nplain line\n", encoding="utf-8")
    (root / "src" / "deep" / "util.py").write_text(
        "def helper():\n    return 'needle'\n", encoding="utf-8"
    )
    (root / ".hidden.py").write_text("needle hidden\n", encoding="utf-8")
    (root / ".hdir").mkdir()
    (root / ".hdir" / "inner.py").write_text("needle\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config.py").write_text("needle git\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01needle bytes")
    return root


def _roots(workspace):
    return [str(workspace)]


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------


def test_find_globstar_matches_any_depth(workspace):
    out = _find_files_fn("**/*.py", workspace_roots=_roots(workspace))
    assert set(out.splitlines()) == {"top.py", "src/app.py", "src/deep/util.py"}


def test_find_single_star_does_not_cross_directories(workspace):
    out = _find_files_fn("src/*.py", workspace_roots=_roots(workspace))
    assert out.splitlines() == ["src/app.py"]


def test_find_under_path_arg(workspace):
    src = str(workspace / "src")
    out = _find_files_fn("*.py", path=src, workspace_roots=_roots(workspace))
    assert out.splitlines() == ["app.py"]
    out = _find_files_fn("deep/*.py", path=src, workspace_roots=_roots(workspace))
    assert out.splitlines() == ["deep/util.py"]


def test_find_hidden_excluded_by_default_included_on_request(workspace):
    out = _find_files_fn("**/*.py", workspace_roots=_roots(workspace))
    assert ".hidden.py" not in out and ".hdir/inner.py" not in out
    out = _find_files_fn("**/*.py", include_hidden=True, workspace_roots=_roots(workspace))
    lines = set(out.splitlines())
    assert ".hidden.py" in lines and ".hdir/inner.py" in lines
    # .git is skipped even when hidden files are requested.
    assert ".git/config.py" not in lines


def test_find_cap_is_loud(workspace):
    many = workspace / "many"
    many.mkdir()
    for i in range(205):
        (many / f"f{i:03d}.txt").write_text("x\n", encoding="utf-8")
    out = _find_files_fn("many/*.txt", workspace_roots=_roots(workspace))
    lines = out.splitlines()
    assert len([ln for ln in lines if ln.startswith("many/")]) == filesystem.FIND_MAX_RESULTS
    assert lines[-1].startswith("[capped at 200")


def test_find_no_match_and_bad_path_arg(workspace):
    roots = _roots(workspace)
    assert "No files matched pattern '*.rs'." in _find_files_fn("*.rs", workspace_roots=roots)
    assert "not a directory" in _find_files_fn(
        "*.py", path=str(workspace / "top.py"), workspace_roots=roots
    )
    assert "does not exist" in _find_files_fn(
        "*.py", path=str(workspace / "nope"), workspace_roots=roots
    )


def test_find_escape_attempts_raise(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    roots = _roots(workspace)
    with pytest.raises(PathContainmentError):
        _find_files_fn("*.py", path=str(outside), workspace_roots=roots)
    with pytest.raises(PathContainmentError):
        _find_files_fn("*.py", path=str(workspace / ".." / "outside"), workspace_roots=roots)


# ---------------------------------------------------------------------------
# search_text
# ---------------------------------------------------------------------------


def test_search_matches_with_file_line_format(workspace):
    out = _search_text_fn("needle", workspace_roots=_roots(workspace))
    assert "src/notes.txt:1: needle in text" in out
    assert "src/deep/util.py:2:     return 'needle'" in out
    # Hidden, .git and binary material stay out by default.
    assert ".hidden.py" not in out and ".git" not in out and "blob.bin" not in out


def test_search_regex_and_case(workspace):
    roots = _roots(workspace)
    out = _search_text_fn(r"def \w+", workspace_roots=roots)
    assert "src/deep/util.py:1: def helper():" in out
    out = _search_text_fn("NEEDLE", workspace_roots=roots)
    assert "src/app.py:3: NEEDLE = 1" in out
    assert "notes.txt" not in out  # lowercase 'needle' is a different match


def test_search_invalid_regex_is_a_clean_error(workspace):
    out = _search_text_fn("[unclosed", workspace_roots=_roots(workspace))
    assert out.startswith("Error: invalid regular expression")


def test_search_file_pattern_filter(workspace):
    out = _search_text_fn("needle", file_pattern="*.txt", workspace_roots=_roots(workspace))
    assert "src/notes.txt:1: needle in text" in out
    assert "util.py" not in out and "app.py" not in out


def test_search_single_file_path(workspace):
    notes = str(workspace / "src" / "notes.txt")
    out = _search_text_fn("needle", path=notes, workspace_roots=_roots(workspace))
    assert out.splitlines() == [f"{notes}:1: needle in text"]


def test_search_match_cap_is_loud(workspace):
    big = workspace / "hits.txt"
    big.write_text("".join(f"hit {i}\n" for i in range(150)), encoding="utf-8")
    out = _search_text_fn("hit", path=str(workspace), workspace_roots=_roots(workspace))
    lines = out.splitlines()
    assert len([ln for ln in lines if ": hit " in ln]) == filesystem.SEARCH_MAX_MATCHES
    assert lines[-1].startswith("[capped at 100 matching lines")


def test_search_scan_cap_is_loud(workspace, monkeypatch):
    folder = workspace / "scan"
    folder.mkdir()
    for i in range(1, 6):
        (folder / f"s{i}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(filesystem, "SEARCH_MAX_FILES_SCANNED", 3)
    out = _search_text_fn("needle", path=str(folder), workspace_roots=_roots(workspace))
    assert "s1.txt:1: needle" in out and "s3.txt:1: needle" in out
    assert "s4.txt" not in out and "s5.txt" not in out
    assert "[stopped after scanning 3 files" in out


def test_search_no_matches(workspace):
    out = _search_text_fn("zzz_absent_string", workspace_roots=_roots(workspace))
    assert out == "No matches found for 'zzz_absent_string'."


def test_search_symlink_escape_is_skipped(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("needle outside\n", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)
    out = _search_text_fn("needle outside", workspace_roots=_roots(workspace))
    assert out == "No matches found for 'needle outside'."


def test_search_escape_attempt_raises(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n", encoding="utf-8")
    with pytest.raises(PathContainmentError):
        _search_text_fn("needle", path=str(outside), workspace_roots=_roots(workspace))


# ---------------------------------------------------------------------------
# Pre-flight parity + registry
# ---------------------------------------------------------------------------


def test_preflight_agrees_with_tools(workspace, tmp_path):
    roots = _roots(workspace)
    assert validate_tool_request("find_files", {"pattern": "**/*.py"}, workspace_roots=roots).is_valid
    assert validate_tool_request(
        "search_text", {"query": "x", "path": str(workspace / "src" / "notes.txt")},
        workspace_roots=roots,
    ).is_valid
    assert not validate_tool_request(
        "find_files", {"pattern": "*.py", "path": str(tmp_path / "outside")},
        workspace_roots=roots,
    ).is_valid
    assert not validate_tool_request(
        "search_text", {"query": "x", "path": str(tmp_path / "outside.txt")},
        workspace_roots=roots,
    ).is_valid


@pytest.mark.asyncio
async def test_registry_membership_and_execution(workspace):
    registry = build_tool_registry()
    names = registry.read_only_names()
    assert "find_files" in names and "search_text" in names
    result = await registry.execute(
        "find_files",
        {"pattern": "src/*.py"},
        workspace_roots=_roots(workspace),
        policies={},
    )
    assert result == "src/app.py"
    result = await registry.execute(
        "search_text",
        {"query": "NEEDLE"},
        workspace_roots=_roots(workspace),
        policies={},
    )
    assert "src/app.py:3: NEEDLE = 1" in result
