"""E10-F06 — the engine-payload assembler's side of the layout contract.

``scripts/engine_payload_layout.json`` is the ONE place the payload
shape is pinned: the assembler (scripts/assemble_engine_payload.py)
builds it, ``desktop_app/electron/engineLifecycle.cjs`` resolves it,
and the Jest suite ``enginePayloadLayout.test.js`` pins the launcher
side against the same file. These tests pin the assembler side:

- a hermetic assembly (provided fake runtime tree, dependency step
  skipped) produces exactly the tree the layout describes;
- the payload interpreter genuinely imports the engine entry point
  (verification is part of assembly, not a hope);
- a runtime tree without the layout's interpreter is refused loudly.

The full dependency-installing assembly is exercised in CI by the
release workflow; here the fake runtime mirrors a real venv-shaped
tree (interpreter symlink + pyvenv.cfg + the test venv's
site-packages linked in), so the payload interpreter genuinely runs
with the project's dependencies — without a download.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "assemble_engine_payload.py"
LAYOUT_PATH = REPO_ROOT / "scripts" / "engine_payload_layout.json"


def _load_assembler():
    spec = importlib.util.spec_from_file_location("assemble_engine_payload", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assembler = _load_assembler()
LAYOUT = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def fake_runtime(tmp_path: Path) -> Path:
    """A portable-runtime-shaped tree standing in for PBS/CI input.

    Mirrors how a real tree is self-contained: ``bin/python3`` plus a
    ``pyvenv.cfg`` (so prefix detection lands on this tree) plus
    ``lib/pythonX.Y/site-packages`` — here linked to the test venv's
    own site-packages, which is where the project deps live.
    """
    runtime = tmp_path / "fake-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python3").symlink_to(sys.executable)
    venv_cfg = Path(sys.prefix) / "pyvenv.cfg"
    if venv_cfg.is_file():
        shutil.copy2(venv_cfg, runtime / "pyvenv.cfg")
    else:  # pragma: no cover - the project venv always carries one
        home = Path(sys.base_prefix) / "bin"
        (runtime / "pyvenv.cfg").write_text(
            f"home = {home}\ninclude-system-site-packages = false\n",
            encoding="utf-8",
        )
    purelib = Path(sysconfig.get_paths()["purelib"])
    site_packages = (
        runtime / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site_packages.parent.mkdir(parents=True, exist_ok=True)
    site_packages.symlink_to(purelib, target_is_directory=True)
    return runtime


def test_layout_names_the_production_shape():
    assert LAYOUT["payloadDirName"] == "engine"
    assert LAYOUT["assembledDir"] == "desktop_app/engine-payload"
    assert LAYOUT["runtimeDir"] == "python"
    assert LAYOUT["appDir"] == "app"
    assert LAYOUT["entryModule"] == "aether_engine.main"
    assert LAYOUT["port"] == 8000
    assert LAYOUT["healthPath"] == "/health"
    assert LAYOUT["pythonExe"]["win32"] == "python.exe"
    assert LAYOUT["pythonExe"]["linux"] == "bin/python3"
    assert LAYOUT["pythonExe"]["darwin"] == "bin/python3"


def test_layout_app_packages_exist_in_the_repo():
    for package in LAYOUT["appPackages"]:
        assert (REPO_ROOT / package).is_dir(), package
    # The worker package matters: the engine spawns `python -m
    # aether_worker` with its own interpreter, so the payload
    # interpreter must be able to import it from the app dir too.
    assert (REPO_ROOT / "aether_worker" / "__main__.py").is_file()


def test_assemble_produces_exactly_the_layout_tree(tmp_path, fake_runtime):
    out = tmp_path / "payload"
    # skip_verify: this test pins what the COPY produces; the import
    # verification has its own test below (and importing legitimately
    # writes __pycache__ next to the copied sources).
    manifest = assembler.assemble(
        "linux", out, runtime_dir=fake_runtime, skip_deps=True, skip_verify=True,
    )
    # Runtime at the layout's coordinates.
    assert (out / LAYOUT["runtimeDir"] / LAYOUT["pythonExe"]["linux"]).is_file()
    # Every app package copied as files, entry points included.
    app_dir = out / LAYOUT["appDir"]
    for package in LAYOUT["appPackages"]:
        assert (app_dir / package).is_dir(), package
    assert (app_dir / "aether_engine" / "main.py").is_file()
    assert (app_dir / "aether_common" / "auth.py").is_file()
    assert (app_dir / "aether_worker" / "__main__.py").is_file()
    # No bytecode caches ride along.
    assert not list(app_dir.rglob("__pycache__"))
    # The manifest describes what was built, in layout terms.
    on_disk = json.loads((out / LAYOUT["manifestFile"]).read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert manifest["platform"] == "linux"
    assert manifest["entry_module"] == LAYOUT["entryModule"]
    assert manifest["app_packages"] == LAYOUT["appPackages"]
    assert manifest["app_dir"] == LAYOUT["appDir"]
    assert manifest["runtime_dir"] == LAYOUT["runtimeDir"]
    assert manifest["python_version"].startswith("3.12.")


def test_verification_imports_the_engine_with_payload_python(tmp_path, fake_runtime):
    out = tmp_path / "payload"
    assembler.assemble("linux", out, runtime_dir=fake_runtime, skip_deps=True)
    # assemble() already verified; call it again explicitly — the
    # payload interpreter, cwd elsewhere, PYTHONPATH=<payload>/app,
    # must import the engine entry point, common and worker packages.
    assembler.verify_payload(LAYOUT, "linux", out)


def test_runtime_without_the_layout_interpreter_is_refused(tmp_path):
    bad = tmp_path / "not-a-runtime"
    bad.mkdir()
    with pytest.raises(assembler.AssemblerError, match="interpreter is missing"):
        assembler.assemble(
            "linux", tmp_path / "payload", runtime_dir=bad, skip_deps=True,
        )


def test_wrong_python_version_is_refused(tmp_path, fake_runtime):
    def fake_runner(cmd, **kwargs):
        class Proc:
            returncode = 0
            stdout = "Python 3.11.9"
            stderr = ""
        return Proc()

    exe = fake_runtime / "bin" / "python3"
    with pytest.raises(assembler.AssemblerError, match="3.12"):
        assembler.check_runtime_version(exe, runner=fake_runner)


def test_unknown_platform_is_refused(tmp_path, fake_runtime):
    with pytest.raises(assembler.AssemblerError, match="Unknown platform"):
        assembler.assemble(
            "amiga", tmp_path / "payload", runtime_dir=fake_runtime, skip_deps=True,
        )
