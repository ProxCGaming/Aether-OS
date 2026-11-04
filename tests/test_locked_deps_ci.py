"""Locked dependencies in CI (Hub Risk #1, closed).

The old requirements-lock.txt was a `pip freeze` from a test venv and
silently missed 8 packages present in requirements.txt (uvicorn,
PySide6, qasync, google-genai, APScheduler, pywin32, ddgs, keyring):
installing from the lock alone produced a broken environment, and CI
had no step that would ever have caught it.

The uv migration replaced the pair with pyproject.toml (intent) +
uv.lock (generated, complete), and the requirements files were
deleted. These tests pin the CI contract that keeps the risk closed:

  * uv.lock exists, pyproject.toml exists, and neither requirements
    file ever comes back;
  * every Python job in ci.yml checks the lock is up to date
    (`uv lock --check`), creates its venv explicitly
    (`uv venv .venv --python 3.12`), and installs only from the
    lock — every `uv sync` in the workflow carries `--locked`, so
    CI can never silently re-resolve and drift;
  * a dedicated deps job installs `--all-extras` into a clean venv
    (the voice extra is locked too, not just the default set) and
    runs an import smoke test naming the packages the old lock
    missed.

Text assertions on the workflow file, in the same spirit as the
other packaging pins: no YAML parser is needed and a workflow edit
that weakens the contract fails here by name.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _ci_text() -> str:
    return CI_YML.read_text(encoding="utf-8")


def test_lock_is_the_single_install_source():
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "uv.lock").is_file()
    assert not (REPO_ROOT / "requirements.txt").exists()
    assert not (REPO_ROOT / "requirements-lock.txt").exists()


def test_ci_never_installs_from_requirements_files():
    text = _ci_text()
    # The workflow header may *mention* the deleted files as history;
    # no step may install from them.
    assert "pip install" not in text
    assert "-r requirements" not in text


def test_every_uv_sync_in_ci_is_locked():
    sync_lines = [
        line.strip()
        for line in _ci_text().splitlines()
        if "uv sync" in line and not line.strip().startswith("#")
    ]
    assert sync_lines, "ci.yml must install Python dependencies"
    for line in sync_lines:
        assert "--locked" in line, f"unlocked uv sync in CI: {line}"


def test_ci_checks_lock_and_creates_venv_in_each_python_job():
    text = _ci_text()
    # deps, python (pytest) and lint jobs each do all three steps.
    assert text.count("uv lock --check") >= 3
    assert text.count("uv venv .venv --python 3.12") >= 3


def test_deps_job_proves_all_extras_importable():
    text = _ci_text()
    assert "uv sync --locked --all-extras" in text
    # The import smoke test names the packages the old lock missed
    # (PySide6/qasync left with the retired UI; pywin32 is
    # Windows-only) plus the voice extra's packages.
    for module in (
        "uvicorn",
        "google.genai",
        "apscheduler",
        "ddgs",
        "keyring",
        "aiosqlite",
        "faster_whisper",
        "kokoro",
        "onnxruntime",
        "silero_vad",
    ):
        assert module in text, f"CI import smoke test is missing {module}"
