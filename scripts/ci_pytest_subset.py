#!/usr/bin/env python3
"""Judge a pytest JUnit XML report by the F-18 subset rule (CI gate).

This is the CI counterpart of .husky/pre-push. The local hook parses
pytest's ``-rf`` summary; CI instead gives pytest ``--junitxml`` and
judges the XML with this script. Both apply the SAME rule over the
SAME baseline (scripts/pytest-known-failures.txt, one node id per
line, ``#`` comments and blank lines ignored):

  - The job fails ONLY when a failed/errored test is NOT listed in
    the baseline. Pre-existing, environment-dependent failures that
    are already recorded never redden CI.
  - A baseline entry whose test now passes is reported as an
    informational note (so the baseline can be trimmed when a fix
    lands) — it NEVER fails the job.

Fail closed, like the hook: a missing/unreadable XML report, or a
report containing no test cases at all, exits non-zero — an
unjudgeable run is never a pass.

Usage:
    python scripts/ci_pytest_subset.py pytest-results.xml
    python scripts/ci_pytest_subset.py report.xml --baseline path/to/baseline.txt

Exit codes: 0 = every failure is within the baseline,
            1 = failures outside the baseline,
            2 = the report could not be judged.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "pytest-known-failures.txt"


def load_baseline(path: Path) -> set[str]:
    """Mirror the hook: `grep -v '^#' file | grep -v '^$'`, as a set."""
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def node_id(classname: str, name: str, root: Path) -> str:
    """Rebuild a pytest node id from JUnit's classname + name.

    pytest writes classname as the dotted path of module + class
    (e.g. ``tests.test_engine_ws.TestEngineWebSocket``) and name as
    the test function (e.g. ``test_provider_save_and_remove``). The
    baseline stores ``tests/test_engine_ws.py::TestEngineWebSocket::
    test_provider_save_and_remove``. The module/class split is found
    by taking the longest dotted prefix that is a real .py file in
    this checkout; everything after it is class names.
    """
    parts = [p for p in classname.split(".") if p]
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i]) + ".py"
        if (root / candidate).is_file():
            rest = parts[i:]
            node = candidate
            if rest:
                node += "::" + "::".join(rest)
            return f"{node}::{name}"
    # No matching file (should not happen in a real checkout): fall
    # back to treating the last component as the class name.
    if len(parts) >= 2:
        return "/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + name
    return classname + "::" + name


def collect_failures(xml_path: Path, root: Path) -> tuple[set[str], int]:
    """Return (failed node ids, total testcase count) from JUnit XML.

    A testcase counts as failed when it carries a <failure> or an
    <error> child — the XML counterpart of the hook's FAILED + ERROR
    summary lines.
    """
    tree = ET.parse(xml_path)
    failures: set[str] = set()
    total = 0
    for case in tree.getroot().iter("testcase"):
        total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failures.add(node_id(case.get("classname", ""), case.get("name", ""), root))
    return failures, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", help="JUnit XML report written by pytest --junitxml")
    parser.add_argument(
        "--baseline",
        default=str(DEFAULT_BASELINE),
        help="known-failures baseline (default: scripts/pytest-known-failures.txt)",
    )
    args = parser.parse_args(argv)

    xml_path = Path(args.junit_xml)
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"ci_pytest_subset: baseline file missing: {baseline_path}", file=sys.stderr)
        print("ci_pytest_subset: cannot judge the suite without it — failing closed.", file=sys.stderr)
        return 2
    if not xml_path.is_file():
        print(f"ci_pytest_subset: JUnit report missing: {xml_path}", file=sys.stderr)
        print("ci_pytest_subset: pytest produced no report — failing closed.", file=sys.stderr)
        return 2
    try:
        failures, total = collect_failures(xml_path, REPO_ROOT)
    except ET.ParseError as exc:
        print(f"ci_pytest_subset: cannot parse {xml_path}: {exc}", file=sys.stderr)
        print("ci_pytest_subset: failing closed.", file=sys.stderr)
        return 2
    if total == 0:
        print("ci_pytest_subset: report contains no test cases — failing closed.", file=sys.stderr)
        return 2

    baseline = load_baseline(baseline_path)
    new_failures = sorted(failures - baseline)
    now_passing = sorted(baseline - failures)

    print(
        f"ci_pytest_subset: {total} test cases, {len(failures)} failed/errored, "
        f"{len(baseline)} baseline entries."
    )
    if now_passing:
        print("ci_pytest_subset: note: baseline entries now passing (information only — not a failure):")
        for entry in now_passing:
            print(f"  NOW PASSING {entry}")
        print("ci_pytest_subset: note: consider trimming those entries from scripts/pytest-known-failures.txt.")
    if new_failures:
        print("ci_pytest_subset: FAILED — failures not in the known baseline:", file=sys.stderr)
        for entry in new_failures:
            print(f"  NEW FAILED {entry}", file=sys.stderr)
        return 1
    print("ci_pytest_subset: OK — every failure is within the known baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
