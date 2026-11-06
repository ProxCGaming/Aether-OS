#!/usr/bin/env python3
"""Check the release version mirrors agree (D4-17).

The version is single-sourced in ``aether_engine/version.py``
(``APP_VERSION``), with ``desktop_app/package.json`` as a pinned
mirror. When a tag is given, it must name the same version. Both
release build jobs call this script — it replaces the ~17-line bash
block that used to be duplicated verbatim in the Windows and Linux
jobs, where any change to the rule had to be made twice,
identically, or the platforms drifted.

Usage:
    python scripts/check_version_consistency.py [tag]

``tag`` may carry a leading ``v`` or be empty (a manual workflow
run without a tag input checks the two mirrors only).

Exit codes: 0 = consistent, 1 = a mismatch (details on stderr).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_ENGINE_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"(.*)"\s*$', re.MULTILINE)


def read_package_version(root: Path) -> str:
    data = json.loads(
        (root / "desktop_app" / "package.json").read_text(encoding="utf-8")
    )
    return str(data["version"])


def read_engine_version(root: Path) -> str:
    text = (root / "aether_engine" / "version.py").read_text(encoding="utf-8")
    match = _ENGINE_VERSION_RE.search(text)
    if match is None:
        raise ValueError("aether_engine/version.py declares no APP_VERSION")
    return match.group(1)


def main(argv: list[str] | None = None, root: Path = REPO_ROOT) -> int:
    args = sys.argv[1:] if argv is None else argv
    tag = (args[0] if args else "").strip()
    expected = tag.removeprefix("v")
    pkg = read_package_version(root)
    engine = read_engine_version(root)
    print(
        f"version check: tag/input: '{expected}'  "
        f"package.json: '{pkg}'  version.py: '{engine}'"
    )
    if expected and expected != pkg:
        print(
            f"::error::Tag v{expected} does not match desktop_app/package.json "
            f"version {pkg} (single-sourced from aether_engine/version.py). "
            "Bump the version everywhere or fix the tag.",
            file=sys.stderr,
        )
        return 1
    if pkg != engine:
        print(
            f"::error::desktop_app/package.json ({pkg}) and "
            f"aether_engine/version.py ({engine}) disagree — the version "
            "mirrors have drifted.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
