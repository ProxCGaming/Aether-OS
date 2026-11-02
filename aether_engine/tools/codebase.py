"""Code-awareness tools (Epic 2, E2-11): project_tree + run_checks.

``project_tree`` is READ_ONLY and containment-checked like the other
filesystem reads. ``run_checks`` is EXECUTE: it runs the repository's
own detected checks (shared detection/parsing in
``aether_common.checks``) — in the worker on the graph path, with this
in-process implementation for direct registry callers. Full LSP/DAP
is explicitly out of scope.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from aether_common.checks import (
    CHECK_TIMEOUT_S,
    detect_checks,
    format_summary,
    parse_result,
)
from aether_engine.tools.paths import effective_roots, resolve_contained
from aether_engine.tools.registry import Tool, ToolRisk

TREE_MAX_DEPTH = 4
TREE_MAX_ENTRIES = 300
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def _project_tree_fn(path: str | None = None, workspace_roots: list | None = None) -> str:
    root = resolve_contained(path, workspace_roots) if path else effective_roots(workspace_roots)[0].resolve()
    if not root.is_dir():
        return f"Error: not a directory: {root}"
    resolved_roots = [r.resolve() for r in effective_roots(workspace_roots)]
    lines = [f"[project_tree: {root}]"]
    count = 0
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal count, truncated
        if depth > TREE_MAX_DEPTH or truncated:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for entry in entries:
            if count >= TREE_MAX_ENTRIES:
                truncated = True
                return
            if entry.is_dir() and entry.name in SKIP_DIRS:
                continue
            if entry.name.startswith(".") and entry.name not in (".aether",):
                continue
            count += 1
            indent = "  " * depth
            lines.append(f"{indent}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                # E2-F02: is_dir() follows symlinks, so a linked
                # directory can point outside the granted roots.
                # Descend only when the RESOLVED directory stays
                # inside a root — the same resolve-then-check rule
                # resolve_contained applies to the starting path.
                # The link's own name is listed above; its outside
                # contents are not disclosed.
                resolved = entry.resolve()
                if any(resolved.is_relative_to(r) for r in resolved_roots):
                    walk(entry, depth + 1)

    walk(root, 0)
    if truncated:
        lines.append(f"[truncated at {TREE_MAX_ENTRIES} entries — pass a subdirectory path to see more]")
    return "\n".join(lines)


def run_checks_locally(root: Path) -> str:
    """The in-process run_checks implementation (worker mirrors it)."""
    checks = detect_checks(root)
    results = []
    for check in checks:
        try:
            completed = subprocess.run(
                check["argv"], cwd=str(root), capture_output=True, text=True,
                timeout=CHECK_TIMEOUT_S, check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            results.append({**parse_result(check["kind"], output, completed.returncode), "name": check["name"]})
        except subprocess.TimeoutExpired:
            results.append({"name": check["name"], "ok": False, "passed": 0, "failed": 0,
                            "errors": 1, "failures": [f"timed out after {CHECK_TIMEOUT_S}s"]})
        except OSError as exc:
            results.append({"name": check["name"], "ok": False, "passed": 0, "failed": 0,
                            "errors": 1, "failures": [f"could not run: {exc}"]})
    return format_summary(str(root), results)


def _run_checks_fn(path: str, workspace_roots: list | None = None) -> str:
    root = resolve_contained(path, workspace_roots)
    if not root.is_dir():
        return f"Error: not a directory: {path}"
    return run_checks_locally(root)


def codebase_tools() -> list[Tool]:
    return [
        Tool(
            name="project_tree",
            description=(
                "Show a bounded tree summary of a directory inside the workspace "
                "(structure only — use read_file for file contents)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to summarise (default: the workspace root)."},
                },
            },
            execute_fn=_project_tree_fn,
            risk=ToolRisk.READ_ONLY,
            wants_workspace_roots=True,
        ),
        Tool(
            name="run_checks",
            description=(
                "Run the repository's own checks (ruff/pytest for Python, jest/oxlint for JS, "
                "as detected from the project files) and return a parsed pass/fail summary. "
                "Use after edits to verify the work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository root directory inside the workspace."},
                },
                "required": ["path"],
            },
            execute_fn=_run_checks_fn,
            risk=ToolRisk.EXECUTE,
            wants_workspace_roots=True,
            timeout_s=CHECK_TIMEOUT_S * 3,
        ),
    ]
