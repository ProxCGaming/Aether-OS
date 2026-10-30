"""Repo check detection + result parsing, shared by engine and worker.

Epic 2, E2-11: ``run_checks`` runs a repository's OWN checks (ruff /
pytest / oxlint / jest) and returns a parsed summary instead of raw
spew. Detection and parsing are pure functions here so the engine's
in-process implementation and the worker's subprocess implementation
cannot drift; only the process execution differs between them.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

#: Per-check wall-clock bound, seconds.
CHECK_TIMEOUT_S = 180
#: How many leading failure lines a summary carries.
MAX_FAILURE_LINES = 10


def detect_checks(root: Path) -> list[dict]:
    """Checks to run for the repo at ``root``: [{name, argv, kind}].

    Conservative: a check is offered only when its marker exists —
    ruff needs Python project markers, pytest needs tests, jest needs
    a package.json test script, oxlint needs its config or dependency.
    """
    checks: list[dict] = []
    is_python = (root / "pyproject.toml").exists() or (root / "setup.cfg").exists() or any(root.glob("*.py"))
    if is_python:
        ruff = shutil.which("ruff")
        checks.append({
            "name": "ruff",
            "kind": "ruff",
            "argv": [ruff, "check", "."] if ruff else [sys.executable, "-m", "ruff", "check", "."],
        })
        has_tests = (root / "tests").is_dir() or any(root.rglob("test_*.py"))
        if has_tests:
            checks.append({
                "name": "pytest",
                "kind": "pytest",
                "argv": [sys.executable, "-m", "pytest", "-q"],
            })
    package_json = root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}
        scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        if isinstance(scripts, dict) and scripts.get("test"):
            checks.append({"name": "jest/npm test", "kind": "jest", "argv": ["npm", "test", "--silent"]})
        dev_deps = pkg.get("devDependencies", {}) if isinstance(pkg, dict) else {}
        if (root / ".oxlintrc.json").exists() or (isinstance(dev_deps, dict) and "oxlint" in dev_deps):
            checks.append({"name": "oxlint", "kind": "oxlint", "argv": ["npx", "oxlint"]})
    return checks


def _count(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else 0


def parse_result(kind: str, output: str, returncode: int) -> dict:
    """Parse one check's output into counts + first failure lines."""
    failures = [
        line for line in output.splitlines()
        if ("FAILED" in line or "error" in line.lower() or ": " in line and "Error" in line)
    ][:MAX_FAILURE_LINES]
    if kind == "pytest":
        return {
            "passed": _count(r"(\d+) passed", output),
            "failed": _count(r"(\d+) failed", output),
            "errors": _count(r"(\d+) errors?", output),
            "ok": returncode == 0,
            "failures": failures,
        }
    if kind == "jest":
        return {
            "passed": _count(r"(\d+) passed", output),
            "failed": _count(r"(\d+) failed", output),
            "errors": 0,
            "ok": returncode == 0,
            "failures": failures,
        }
    if kind in ("ruff", "oxlint"):
        issues = len(re.findall(r"^\S+:\d+:\d+:", output, flags=re.MULTILINE))
        return {
            "passed": 0,
            "failed": issues,
            "errors": 0,
            "ok": returncode == 0,
            "failures": [line for line in output.splitlines() if line.strip()][:MAX_FAILURE_LINES],
        }
    return {"passed": 0, "failed": 0, "errors": 0, "ok": returncode == 0, "failures": failures}


def format_summary(root: str, results: list[dict]) -> str:
    """Bounded, truthful summary text for the model."""
    if not results:
        return f"No checks detected for {root} (no ruff/pytest/jest/oxlint markers found)."
    lines = [f"Checks for {root}:"]
    for result in results:
        status = "PASS" if result["ok"] else "FAIL"
        counts = f"{result['passed']} passed, {result['failed']} failed"
        if result.get("errors"):
            counts += f", {result['errors']} errors"
        lines.append(f"- {result['name']}: {status} ({counts})")
        if not result["ok"]:
            for failure in result.get("failures", [])[:5]:
                lines.append(f"    {failure}")
    return "\n".join(lines)
