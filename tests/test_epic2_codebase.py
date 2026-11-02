"""Epic 2, E2-11: project_tree + run_checks.

Detection is conservative (markers required), parsing is truthful
(counts from the tools' own summary lines), and run_checks against a
fixture repo returns a summary that matches reality.
"""

from __future__ import annotations

import pytest

from aether_common.checks import detect_checks, parse_result
from aether_engine.tools.codebase import _project_tree_fn, run_checks_locally
from aether_engine.tools.paths import PathContainmentError


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("{}", encoding="utf-8")
    return root


def test_tree_skips_noise_and_marks_dirs(repo, tmp_path):
    out = _project_tree_fn(str(repo), workspace_roots=[str(tmp_path)])
    assert "src/" in out and "app.py" in out and "pyproject.toml" in out
    assert "node_modules" not in out
    with pytest.raises(PathContainmentError):
        _project_tree_fn(str(tmp_path.parent), workspace_roots=[str(tmp_path)])


def test_detection_markers(repo, tmp_path):
    kinds = {c["kind"] for c in detect_checks(repo)}
    assert kinds == {"ruff", "pytest"}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_checks(empty) == []


def test_parse_pytest_and_ruff():
    parsed = parse_result("pytest", ".... 3 passed, 1 failed in 0.5s", 1)
    assert parsed["passed"] == 3 and parsed["failed"] == 1 and parsed["ok"] is False
    clean = parse_result("ruff", "All checks passed!", 0)
    assert clean["ok"] is True and clean["failed"] == 0
    dirty = parse_result("ruff", "a.py:1:1: F401 unused\nb.py:2:3: E501 long", 1)
    assert dirty["failed"] == 2 and dirty["ok"] is False


def test_run_checks_against_fixture_repo(repo):
    summary = run_checks_locally(repo)
    assert "ruff" in summary and "pytest" in summary
    assert "1 passed" in summary  # the fixture's one real test


@pytest.mark.asyncio
async def test_worker_run_checks(tmp_path):
    from aether_worker.__main__ import run_worker

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    out = await run_worker({"tool_name": "run_checks", "args": {"path": str(root)}})
    assert "pytest" in out["summary"] and "1 passed" in out["summary"]


def test_registry_membership():
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry()
    assert "project_tree" in registry.read_only_names()
    assert registry.get_tool("run_checks").risk.value == "execute"
