"""Wave 3 (D4) — the CI/release gate infrastructure pins.

Text assertions on the workflow files, in the same spirit as
tests/test_locked_deps_ci.py: a workflow edit that weakens one of
these contracts fails here by name.

  D4-05  release.yml runs the test gates for the tagged commit in a
         dedicated gate job, and the publish job needs it — a tag
         on a red commit produces no GitHub Release.
  D4-12  ci.yml declares least-privilege permissions (contents:
         read) instead of inheriting the repo default token grant.
  D4-13  coverage measures the privileged code too: pytest covers
         aether_common alongside aether_engine, and Jest's
         collectCoverageFrom reaches electron/*.cjs, main.cjs and
         preload.cjs.
  D4-17  the release version-consistency check exists ONCE, as
         scripts/check_version_consistency.py, called by both build
         jobs — not as two verbatim bash copies that can drift.
  D4-16  scripts/run-related-jest.sh (the lint-staged gate) passes a
         failing run ONLY when jest found no tests at all — never
         when real failures share the output with that phrase.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
JEST_CONFIG = REPO_ROOT / "desktop_app" / "jest.config.cjs"
VERSION_SCRIPT = REPO_ROOT / "scripts" / "check_version_consistency.py"
RELATED_JEST = REPO_ROOT / "scripts" / "run-related-jest.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- D4-12: ci.yml least-privilege permissions ---------------------------


def test_ci_declares_least_privilege_permissions():
    text = _read(CI_YML)
    assert "\npermissions:\n  contents: read\n" in text


# --- D4-13: coverage measures the privileged code -------------------------


def test_pytest_coverage_includes_aether_common():
    assert "--cov=aether_common" in _read(CI_YML)


def test_jest_coverage_reaches_the_main_process_files():
    text = _read(JEST_CONFIG)
    assert "electron/**/*.cjs" in text
    assert "'main.cjs'" in text
    assert "'preload.cjs'" in text


# --- D4-05 + D4-17: release gates and the single version check ------------


def test_release_has_a_gate_job_running_both_suites():
    text = _read(RELEASE_YML)
    assert "\n  gate:\n" in text
    gate_block = text.split("\n  gate:\n", 1)[1].split("\n  build-windows:", 1)[0]
    assert "ci_pytest_subset.py" in gate_block  # pytest, judged by the subset rule
    assert "npm run test:coverage" in gate_block or "npm test" in gate_block  # Jest


def test_release_publish_needs_the_gate():
    text = _read(RELEASE_YML)
    assert "needs: [gate, build-windows, build-linux]" in text


def test_version_check_is_a_single_shared_script_called_by_both_jobs():
    text = _read(RELEASE_YML)
    assert text.count("python ../scripts/check_version_consistency.py") == 2  # one call per build job
    # The duplicated inline block is gone: no job sed-parses version.py.
    assert "s/^APP_VERSION" not in text
    assert VERSION_SCRIPT.is_file()


# --- check_version_consistency.py behaviour --------------------------------


def _load_version_script():
    spec = importlib.util.spec_from_file_location(
        "check_version_consistency", VERSION_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, pkg_version: str, engine_version: str) -> Path:
    (tmp_path / "desktop_app").mkdir(parents=True)
    (tmp_path / "aether_engine").mkdir(parents=True)
    (tmp_path / "desktop_app" / "package.json").write_text(
        f'{{"name": "aether-os-desktop", "version": "{pkg_version}"}}',
        encoding="utf-8",
    )
    (tmp_path / "aether_engine" / "version.py").write_text(
        f'APP_VERSION = "{engine_version}"\n', encoding="utf-8"
    )
    return tmp_path


def test_version_script_accepts_a_consistent_tree(tmp_path):
    module = _load_version_script()
    root = _repo(tmp_path, "0.3.5", "0.3.5")
    assert module.main(["v0.3.5"], root=root) == 0
    assert module.main(["0.3.5"], root=root) == 0  # bare version also fine


def test_version_script_rejects_a_tag_that_disagrees_with_the_package(tmp_path, capsys):
    module = _load_version_script()
    root = _repo(tmp_path, "0.3.5", "0.3.5")
    assert module.main(["v0.4.0"], root=root) == 1
    assert "does not match" in capsys.readouterr().err


def test_version_script_rejects_drifted_mirrors(tmp_path, capsys):
    module = _load_version_script()
    root = _repo(tmp_path, "0.3.5", "0.3.6")
    assert module.main(["v0.3.5"], root=root) == 1
    assert "disagree" in capsys.readouterr().err


def test_version_script_manual_run_without_a_tag_checks_the_mirrors_only(tmp_path):
    module = _load_version_script()
    root = _repo(tmp_path, "0.3.5", "0.3.5")
    assert module.main([""], root=root) == 0
    drifted = _repo(tmp_path / "drift", "0.3.5", "0.3.6")
    assert module.main([""], root=drifted) == 1


# --- D4-16: run-related-jest.sh honesty ------------------------------------


def _run_related_jest(tmp_path: Path, canned_output: str, canned_status: int) -> int:
    """Run the real gate script with a stubbed npx on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "npx"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$CANNED_OUTPUT"\nexit "$CANNED_STATUS"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CANNED_OUTPUT": canned_output,
        "CANNED_STATUS": str(canned_status),
    }
    proc = subprocess.run(
        ["bash", str(RELATED_JEST), "desktop_app/src/components/Foo.jsx"],
        check=False,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode


def test_related_jest_passes_when_no_related_tests_exist(tmp_path):
    # The legitimate case the script exists for: jest ran, found no
    # related tests for the staged files, nothing failed.
    output = "No tests found, exiting with code 1\n"
    assert _run_related_jest(tmp_path, output, 1) == 0


def test_related_jest_fails_when_real_failures_share_the_output(tmp_path):
    # D4-16: one pattern's lookup printing "No tests found" must not
    # launder a genuine failure in the same combined run into a pass.
    output = (
        "No tests found for pattern A\n"
        "FAIL src/components/__tests__/Foo.test.jsx\n"
        "  ● Foo › renders\n"
        "Tests:       1 failed, 3 passed, 4 total\n"
    )
    assert _run_related_jest(tmp_path, output, 1) == 1


def test_related_jest_passes_a_green_run_and_fails_a_plain_failure(tmp_path):
    assert _run_related_jest(tmp_path, "Tests: 4 passed, 4 total\n", 0) == 0
    assert _run_related_jest(tmp_path, "FAIL x\nTests: 1 failed\n", 1) == 1
