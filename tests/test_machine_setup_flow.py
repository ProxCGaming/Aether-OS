"""Machine setup/run flow — the contract that keeps new and old
machines reconciled to the lockfiles.

The failure this guards against happened on the owner's PC on
2026-11-13: a `git pull` updated the source but nothing updated the
environment, so a stale `node_modules` predating `remark-gfm` made
Vite fail while every test stayed green (CI gained a vite-build step
for its side of that blind spot in `d7098c09`). The local half of the
fix is one idempotent setup script per platform family — new-machine
setup and post-pull update are the same command — plus a run script
that starts the engine and the app from the terminal.

These tests pin the scripts' step lists the same way
`test_locked_deps_ci.py` pins the workflow: text assertions, so a
future edit that drops a step, unlocks an install, or moves the
`npm ci` out of `desktop_app/` fails here by name.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
SETUP_PS1 = REPO_ROOT / "scripts" / "setup.ps1"
RUN_SH = REPO_ROOT / "scripts" / "run.sh"
RUN_PS1 = REPO_ROOT / "scripts" / "run.ps1"

SETUP_SCRIPTS = (SETUP_SH, SETUP_PS1)
RUN_SCRIPTS = (RUN_SH, RUN_PS1)
ALL_SCRIPTS = SETUP_SCRIPTS + RUN_SCRIPTS


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """The scripts' header comments describe the steps in prose, so
    ordering assertions must run on code lines only (both script
    dialects comment with #)."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_setup_and_run_scripts_exist_for_both_platforms():
    for path in ALL_SCRIPTS:
        assert path.is_file(), f"missing flow script: {path.name}"


def test_setup_installs_only_from_the_locks():
    for path in SETUP_SCRIPTS:
        text = _read(path)
        # The venv is created at the exact CPython pinned in
        # .python-version (15 Nov 2026: scripts pass the pin file's
        # value to uv venv, never a floating "3.12").
        assert "uv venv" in text and ".python-version" in text, path.name
        assert '--python "$EXPECTED_PYTHON"' in text or "--python $expectedPython" in text, path.name
        assert "uv sync --locked --all-extras" in text, path.name
        assert "npm ci" in text, path.name
        # Nothing may rewrite a lockfile as a side effect of setup.
        assert "npm install" not in text, path.name
        assert "pip install" not in text, path.name
        for line in text.splitlines():
            if "uv sync" in line and not line.strip().startswith("#"):
                assert "--locked" in line, f"unlocked uv sync in {path.name}: {line}"


def test_setup_npm_ci_runs_inside_desktop_app():
    # The 2026-11-13 incident had a second half: `npm ci` run at the
    # repo root installs the husky tooling package and does nothing
    # for the desktop app. The script must enter desktop_app first.
    for path in SETUP_SCRIPTS:
        code = _code_only(_read(path))
        assert code.index("desktop_app") < code.index("npm ci"), path.name


def test_setup_smoke_checks_match_ci():
    for path in SETUP_SCRIPTS:
        text = _read(path)
        # Same version pin and import list as the deps job in ci.yml.
        assert "version_info" in text and "(3, 12)" in text, path.name
        for module in ("uvicorn", "faster_whisper", "silero_vad"):
            assert module in text, f"{path.name} smoke list missing {module}"
        # A vite build (catches missing/undeclared JS imports) and a
        # full pytest collection (catches engine import errors).
        assert "npm run build" in text, path.name
        assert "--collect-only" in text, path.name


def test_setup_checks_node_24_before_installing():
    for path in SETUP_SCRIPTS:
        text = _read(path)
        code = _code_only(text)
        # The expected Node is read from .nvmrc (the pin file) and
        # compared exactly, before any install step runs.
        assert ".nvmrc" in text, path.name
        assert code.index(".nvmrc") < code.index("uv sync"), path.name


def test_run_scripts_start_engine_wait_for_health_then_launch():
    for path in RUN_SCRIPTS:
        text = _read(path)
        assert "run_engine.py" in text, path.name
        assert "8000/health" in text, path.name
        assert "electron:dev" in text, path.name
        assert text.index("run_engine.py") < text.index("electron:dev"), path.name
        # The standing QA warning rides in the script header: a
        # pre-started engine masks the packaged app's own startup.
        assert "PACKAGED" in text, path.name


def test_node_version_is_pinned_where_machines_read_it():
    # Exact pin since 15 Nov 2026: .nvmrc and the desktop manifest's
    # engines must carry the SAME exact Node version — no line pins.
    nvmrc = (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    assert nvmrc == "24.22.1"
    package = json.loads(_read(REPO_ROOT / "desktop_app" / "package.json"))
    assert package["engines"]["node"] == nvmrc


def test_setup_clean_installs_on_lock_fingerprint_mismatch():
    # Owner directive, 15 Nov 2026: a machine whose locks/pins moved
    # since its last setup is wiped and rebuilt, never patched in
    # place. Both setup scripts must carry the fingerprint + stamp
    # + wipe of every environment the locks feed.
    for path in SETUP_SCRIPTS:
        text = _read(path)
        assert ".aether-setup-stamp" in text, path.name
        assert "uv.lock" in text and "package-lock.json" in text, path.name
        assert "engine-payload" in text, path.name
        assert "rm -rf" in text or "Remove-Item" in text, path.name


def test_update_scripts_pull_then_run_setup():
    # The one-command update path for source machines: pull
    # (fast-forward only, so local divergence is never merged over)
    # then the setup flow, whose fingerprint rule clean-installs
    # when the pull moved the locks.
    for name, setup_name in (("update.sh", "setup.sh"), ("update.ps1", "setup.ps1")):
        path = REPO_ROOT / "scripts" / name
        assert path.is_file(), f"missing update script: {name}"
        text = _read(path)
        assert "git pull --ff-only" in text, name
        assert setup_name in text, name
        assert text.index("git pull") < text.index(setup_name), name


def test_script_line_endings_are_pinned():
    attrs = _read(REPO_ROOT / ".gitattributes")
    assert "*.sh" in attrs and "eol=lf" in attrs
    assert "*.ps1" in attrs and "eol=crlf" in attrs


def test_readme_points_at_the_scripts():
    readme = _read(REPO_ROOT / "README.md")
    assert "scripts/setup.sh" in readme
    assert "scripts/run.sh" in readme


def test_powershell_scripts_are_pure_ascii():
    """Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI
    (Windows-1252), where the UTF-8 bytes of an em dash include 0x94
    — a closing smart quote — so one "—" inside a string ended the
    string early and run.ps1 failed to PARSE at all on the owner's
    PC (15 Nov 2026: blank, frozen terminal, nothing executed; the
    engine and venv were healthy). Every .ps1 must stay pure ASCII
    so 5.1 and pwsh read it identically."""
    offenders = []
    for script in sorted((REPO_ROOT / "scripts").glob("*.ps1")):
        data = script.read_bytes()
        bad = sorted({b for b in data if b > 127})
        if bad:
            offenders.append(f"{script.name}: bytes {[hex(b) for b in bad]}")
    assert not offenders, "non-ASCII bytes in PowerShell scripts: " + "; ".join(offenders)
