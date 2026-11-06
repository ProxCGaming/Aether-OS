"""D4-04 — covering tests for scripts/ci_pytest_subset.py, the CI judge.

The judge applies the F-18 subset rule to a JUnit XML report: the
job fails only on failures outside scripts/pytest-known-failures.txt.
Its core is a heuristic (rebuilding pytest node ids from JUnit
classname+name), exactly the kind of logic that fails silently — a
mis-rebuilt id can make a real failure string-match a baseline entry
(false PASS of the gate) or a baseline failure report as new (false
block). These tests pin the id rebuild, the failure collection, the
fail-closed exits, and the D4-15 reporting rule:

  - a baseline entry whose test RAN and passed is "now passing"
    (information only, safe to trim);
  - a baseline entry ABSENT from the report (deselected, file
    deleted, collection error) is "not run" — it must never be
    reported as passing, because trimming it would silently
    un-guard a real known failure.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_pytest_subset.py"


def _load_judge():
    spec = importlib.util.spec_from_file_location("ci_pytest_subset", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


judge = _load_judge()

# Gate-level cases run through main(), which rebuilds ids against
# the real checkout — so their file part must be a file that exists
# in the repo. This very test file serves.
PASS_ID = "tests/test_ci_pytest_subset.py::test_some_passing_case"
FAIL_ID = "tests/test_ci_pytest_subset.py::TestGroup::test_some_failing_case"


def _xml(tmp_path: Path, cases: str) -> Path:
    path = tmp_path / "report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="0">{cases}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _case(node: str, inner: str = "") -> str:
    parts = node.split("::")
    classname = parts[0].replace("/", ".").removesuffix(".py")
    name = parts[-1]
    if len(parts) == 3:
        classname = f"{classname}.{parts[1]}"
    return f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'


def _baseline(tmp_path: Path, entries: list[str]) -> Path:
    path = tmp_path / "baseline.txt"
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, cases: str, baseline_entries: list[str]) -> int:
    xml = _xml(tmp_path, cases)
    baseline = _baseline(tmp_path, baseline_entries)
    return judge.main([str(xml), "--baseline", str(baseline)])


# --- baseline loading ---------------------------------------------------


def test_load_baseline_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "b.txt"
    path.write_text("# header\n\n  tests/test_a.py::test_x  \n# note\ntests/test_b.py::test_y\n")
    assert judge.load_baseline(path) == {
        "tests/test_a.py::test_x",
        "tests/test_b.py::test_y",
    }


# --- node id rebuild ------------------------------------------------------


def test_node_id_module_level_test(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("")
    assert (
        judge.node_id("tests.test_sample", "test_ok", tmp_path)
        == "tests/test_sample.py::test_ok"
    )


def test_node_id_class_based_test(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("")
    assert (
        judge.node_id("tests.test_sample.TestGroup", "test_bad", tmp_path)
        == "tests/test_sample.py::TestGroup::test_bad"
    )


def test_node_id_prefers_the_longest_real_file_prefix(tmp_path):
    # Both tests/test_a.py and tests/test_a/test_b.py exist as files;
    # the class split must be found against the LONGEST dotted prefix
    # that is a real .py file, or ids silently mismatch the baseline.
    (tmp_path / "tests" / "test_a").mkdir(parents=True)
    (tmp_path / "tests" / "test_a.py").write_text("")
    (tmp_path / "tests" / "test_a" / "test_b.py").write_text("")
    assert (
        judge.node_id("tests.test_a.test_b.TestC", "test_x", tmp_path)
        == "tests/test_a/test_b.py::TestC::test_x"
    )


def test_node_id_fallback_when_no_file_matches(tmp_path):
    # No matching file in this checkout: the documented fallback
    # treats the last component as the class name.
    assert (
        judge.node_id("no.such.mod.Thing", "test_a", tmp_path)
        == "no/such/mod.py::Thing::test_a"
    )


# --- failure collection ---------------------------------------------------


def test_collect_counts_failures_and_errors_and_tracks_what_ran(tmp_path):
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("")
    ok = "tests/test_sample.py::test_ok"
    bad = "tests/test_sample.py::TestGroup::test_bad"
    err = "tests/test_sample.py::test_err"
    xml = _xml(
        tmp_path,
        _case(ok)
        + _case(bad, "<failure>boom</failure>")
        + _case(err, "<error>bang</error>")
        + _case("tests/test_sample.py::test_skip", "<skipped/>"),
    )
    failures, total, ran = judge.collect_results(xml, root)
    assert total == 4
    assert failures == {bad, err}
    # D4-15's raw material: every id the report CONTAINS, pass or
    # fail — the judge can only call a baseline entry "now passing"
    # when its id is in this set.
    assert ran == {ok, bad, err, "tests/test_sample.py::test_skip"}


# --- gate verdicts ----------------------------------------------------------


def test_all_failures_within_baseline_passes(tmp_path):
    assert _run(tmp_path, _case(FAIL_ID, "<failure>x</failure>"), [FAIL_ID]) == 0


def test_failure_outside_baseline_blocks(tmp_path):
    assert _run(tmp_path, _case(FAIL_ID, "<failure>x</failure>"), []) == 1


def test_clean_run_passes(tmp_path):
    assert _run(tmp_path, _case(PASS_ID), [FAIL_ID]) == 0


def test_missing_report_fails_closed(tmp_path):
    baseline = _baseline(tmp_path, [])
    assert judge.main([str(tmp_path / "nope.xml"), "--baseline", str(baseline)]) == 2


def test_missing_baseline_fails_closed(tmp_path):
    xml = _xml(tmp_path, _case(PASS_ID))
    assert judge.main([str(xml), "--baseline", str(tmp_path / "nope.txt")]) == 2


def test_empty_report_fails_closed(tmp_path):
    assert _run(tmp_path, "", []) == 2


def test_unparseable_report_fails_closed(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites><oops", encoding="utf-8")
    baseline = _baseline(tmp_path, [])
    assert judge.main([str(bad), "--baseline", str(baseline)]) == 2


# --- D4-15: never-ran baseline entries are NOT "now passing" -----------------


def test_ran_and_passed_baseline_entry_is_reported_now_passing(tmp_path, capsys):
    rc = _run(tmp_path, _case(PASS_ID), [PASS_ID])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"NOW PASSING {PASS_ID}" in out


def test_still_failing_baseline_entry_is_not_reported_as_passing(tmp_path, capsys):
    rc = _run(tmp_path, _case(FAIL_ID, "<failure>x</failure>"), [FAIL_ID])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOW PASSING" not in out
    assert "NOT RUN" not in out


def test_never_ran_baseline_entry_is_reported_not_run_never_passing(tmp_path, capsys):
    # The baseline entry's test is ABSENT from the report entirely
    # (deselected / file deleted / collection error). D4-15: it must
    # be reported as not run, and the output must not invite trimming
    # it as though a fix had landed.
    rc = _run(tmp_path, _case(PASS_ID), [FAIL_ID])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOW PASSING" not in out
    assert f"NOT RUN {FAIL_ID}" in out
