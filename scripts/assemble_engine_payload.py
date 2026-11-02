#!/usr/bin/env python3
"""Assemble the bundled engine payload for the desktop distributable.

E10-F06: the packaged Aether-OS app must carry its Python engine as
FILES (owner-confirmed design, 2 Nov 2026 — a portable interpreter +
the engine's code and libraries; NOT a frozen single executable, so
future premium feature packs can join the client as files too).

What this script produces (the layout is pinned in ONE place —
``scripts/engine_payload_layout.json``, which the Electron launcher
and the tests on both sides are also pinned against)::

    <output>/                     (default: desktop_app/engine-payload)
        python/                   portable CPython runtime tree
            python.exe            (win32)  |  bin/python3 (linux/darwin)
        app/                      engine code, as plain files
            aether_engine/  aether_common/  aether_worker/
        payload-manifest.json     what was assembled, from what

electron-builder copies <output> into the app's resources as
``resources/engine`` (see ``extraResources`` in
desktop_app/package.json); ``desktop_app/electron/engineLifecycle.cjs``
spawns ``python -m aether_engine.main`` from it with PYTHONPATH=<app>.

Runtime acquisition, in priority order:

  --runtime-dir DIR     copy an existing portable-python tree (DIR is
                        the tree root: it contains python.exe or
                        bin/python3). This is how CI mirrors / tests
                        supply a runtime without a download.
  --runtime-archive F   extract a python-build-standalone
                        ``*-install_only.tar.gz`` (its top-level
                        ``python/`` directory becomes the tree).
  (neither)             download the pinned python-build-standalone
                        release for --platform (see RUNTIME_PIN below).
                        Needs network; the pin is deliberate — bump
                        RUNTIME_PIN + CPYTHON_VERSION together and
                        re-verify, never float to "latest".

Dependencies are the project's own [project] dependencies from
pyproject.toml, installed INTO the payload runtime with
``uv pip install --python <payload python> --system`` so platform
markers (e.g. pywin32) resolve against the payload interpreter.
Assemble on the target platform (the release workflow's matrix does
exactly that): wheels are platform-specific.

The engine's own configuration needs nothing from this payload: user
config, secrets and data live under ~/.aether regardless of CWD, and
the auth token agreement is CWD-relative ``.run/engine.token`` — the
launcher sets the spawned engine's CWD, this script does not care.

Out of scope by design: Playwright browser binaries and the optional
voice extra are runtime downloads the engine already manages; the
payload carries the libraries, not downloaded model/browser data.

Usage:
    python scripts/assemble_engine_payload.py [--platform win32]
        [--output DIR] [--runtime-dir DIR | --runtime-archive FILE]
        [--skip-deps] [--skip-verify]

Stdlib only (plus the ``uv`` binary for the dependency step).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAYOUT_PATH = Path(__file__).resolve().parent / "engine_payload_layout.json"

# Pinned portable-runtime release (python-build-standalone, astral-sh).
# CPython must satisfy pyproject's requires-python (>=3.12,<3.13).
RUNTIME_PIN = "20261026"
CPYTHON_VERSION = "3.12.16"
RUNTIME_TRIPLES = {
    "win32": "x86_64-pc-windows-msvc",
    "linux": "x86_64-unknown-linux-gnu",
    # The release matrix builds Windows + Linux today; darwin is the
    # Intel triple — an arm64 macOS matrix entry would pin
    # aarch64-apple-darwin instead.
    "darwin": "x86_64-apple-darwin",
}
RUNTIME_URL_TEMPLATE = (
    "https://github.com/astral-sh/python-build-standalone/releases/"
    "download/{tag}/cpython-{version}+{tag}-{triple}-install_only.tar.gz"
)

HOST_PLATFORMS = {"win32": "win32", "linux": "linux", "darwin": "darwin"}


class AssemblerError(RuntimeError):
    """A loud, expected assembly failure (bad runtime, failed step)."""


def load_layout(path: Path = LAYOUT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def runtime_exe_rel(layout: dict, platform: str) -> Path:
    """Path of the interpreter INSIDE the runtime tree, per layout."""
    return Path(layout["runtimeDir"]) / Path(layout["pythonExe"][platform])


def default_platform() -> str:
    plat = HOST_PLATFORMS.get(sys.platform)
    if plat is None:
        raise AssemblerError(f"Unsupported host platform: {sys.platform}")
    return plat


def _copy_tree_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def _extract_runtime_archive(archive: Path, python_dir: Path) -> None:
    """Extract a python-build-standalone install_only tarball.

    The tarball's single top-level ``python/`` directory IS the
    runtime tree; its contents land in ``python_dir``.
    """
    with tempfile.TemporaryDirectory(prefix="aether-runtime-") as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        tree = staging / "python"
        if not tree.is_dir():
            raise AssemblerError(
                f"Runtime archive {archive} has no top-level python/ tree"
            )
        _copy_tree_contents(tree, python_dir)


def _download_runtime(platform: str, dest_archive: Path) -> str:
    triple = RUNTIME_TRIPLES[platform]
    url = RUNTIME_URL_TEMPLATE.format(
        tag=RUNTIME_PIN, version=CPYTHON_VERSION, triple=triple
    )
    req = urllib.request.Request(url, headers={"User-Agent": "aether-payload-assembler"})
    print(f"Downloading portable runtime: {url}")
    with urllib.request.urlopen(req) as resp, open(dest_archive, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return f"python-build-standalone {RUNTIME_PIN} ({triple})"


def acquire_runtime(
    layout: dict,
    platform: str,
    out_dir: Path,
    runtime_dir: Path | None = None,
    runtime_archive: Path | None = None,
) -> str:
    """Place the runtime tree at <out>/<runtimeDir>; return its source label."""
    python_dir = out_dir / layout["runtimeDir"]
    if runtime_dir is not None:
        if not runtime_dir.is_dir():
            raise AssemblerError(f"--runtime-dir is not a directory: {runtime_dir}")
        _copy_tree_contents(runtime_dir, python_dir)
        source = f"provided runtime dir ({runtime_dir})"
    elif runtime_archive is not None:
        if not runtime_archive.is_file():
            raise AssemblerError(f"--runtime-archive not found: {runtime_archive}")
        _extract_runtime_archive(runtime_archive, python_dir)
        source = f"provided runtime archive ({runtime_archive.name})"
    else:
        with tempfile.TemporaryDirectory(prefix="aether-runtime-dl-") as tmp:
            archive = Path(tmp) / "runtime.tar.gz"
            source = _download_runtime(platform, archive)
            _extract_runtime_archive(archive, python_dir)
    exe = out_dir / runtime_exe_rel(layout, platform)
    if not exe.is_file():
        raise AssemblerError(
            f"Runtime tree placed but the interpreter is missing at {exe} "
            f"(layout expects {runtime_exe_rel(layout, platform)})"
        )
    return source


def check_runtime_version(python_exe: Path, runner=subprocess.run) -> str:
    proc = runner(
        [str(python_exe), "--version"],
        capture_output=True, text=True, timeout=60,
    )
    version = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or not version.startswith("Python 3.12."):
        raise AssemblerError(
            f"Payload interpreter must be CPython 3.12.x "
            f"(pyproject requires-python >=3.12,<3.13); got: {version!r}"
        )
    return version.removeprefix("Python ").strip()


def copy_app_packages(layout: dict, out_dir: Path, repo_root: Path = REPO_ROOT) -> None:
    app_dir = out_dir / layout["appDir"]
    for package in layout["appPackages"]:
        src = repo_root / package
        if not src.is_dir():
            raise AssemblerError(f"App package missing from repo: {src}")
        shutil.copytree(
            src,
            app_dir / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


def project_dependencies(repo_root: Path = REPO_ROOT) -> list[str]:
    with open(repo_root / "pyproject.toml", "rb") as fh:
        project = tomllib.load(fh)["project"]
    return list(project.get("dependencies", []))


def install_dependencies(
    layout: dict,
    platform: str,
    out_dir: Path,
    repo_root: Path = REPO_ROOT,
    runner=subprocess.run,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise AssemblerError(
            "The dependency step needs the `uv` binary on PATH "
            "(the project's own toolchain — see .github/workflows/ci.yml)"
        )
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    deps = project_dependencies(repo_root)
    if not deps:
        raise AssemblerError("pyproject.toml declares no [project] dependencies")
    with tempfile.TemporaryDirectory(prefix="aether-deps-") as tmp:
        req = Path(tmp) / "requirements.txt"
        req.write_text("\n".join(deps) + "\n", encoding="utf-8")
        cmd = [
            uv, "pip", "install",
            "--python", str(python_exe),
            "--system",
            "--requirement", str(req),
        ]
        print(f"Installing {len(deps)} project dependencies into the payload runtime...")
        proc = runner(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise AssemblerError(f"Dependency installation failed:\n{tail}")


def verify_payload(
    layout: dict,
    platform: str,
    out_dir: Path,
    runner=subprocess.run,
) -> None:
    """The payload interpreter must import the engine entry point."""
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    app_dir = out_dir / layout["appDir"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(app_dir)
    with tempfile.TemporaryDirectory(prefix="aether-verify-") as cwd:
        proc = runner(
            [
                str(python_exe), "-c",
                ("import aether_engine.main, aether_common, aether_worker; "
                 "print('payload import ok')"),
            ],
            capture_output=True, text=True, timeout=300, cwd=cwd, env=env,
        )
    if proc.returncode != 0 or "payload import ok" not in proc.stdout:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise AssemblerError(f"Payload verification (engine import) failed:\n{tail}")


def write_manifest(
    layout: dict,
    platform: str,
    out_dir: Path,
    runtime_source: str,
    python_version: str,
) -> dict:
    manifest = {
        "format": 1,
        "platform": platform,
        "python_version": python_version,
        "runtime_source": runtime_source,
        "runtime_dir": layout["runtimeDir"],
        "app_dir": layout["appDir"],
        "app_packages": list(layout["appPackages"]),
        "entry_module": layout["entryModule"],
        "assembled_at": datetime.now(UTC).isoformat(),
        "assembled_by": "scripts/assemble_engine_payload.py",
    }
    (out_dir / layout["manifestFile"]).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def assemble(
    platform: str,
    out_dir: Path,
    runtime_dir: Path | None = None,
    runtime_archive: Path | None = None,
    skip_deps: bool = False,
    skip_verify: bool = False,
    repo_root: Path = REPO_ROOT,
    runner=subprocess.run,
) -> dict:
    layout = load_layout()
    if platform not in layout["pythonExe"]:
        raise AssemblerError(f"Unknown platform: {platform}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    runtime_source = acquire_runtime(
        layout, platform, out_dir,
        runtime_dir=runtime_dir, runtime_archive=runtime_archive,
    )
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    python_version = check_runtime_version(python_exe, runner=runner)
    copy_app_packages(layout, out_dir, repo_root=repo_root)
    if not skip_deps:
        install_dependencies(layout, platform, out_dir, repo_root=repo_root, runner=runner)
    manifest = write_manifest(layout, platform, out_dir, runtime_source, python_version)
    if not skip_verify:
        verify_payload(layout, platform, out_dir, runner=runner)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble the bundled engine payload (E10-F06).",
    )
    parser.add_argument("--platform", choices=sorted(RUNTIME_TRIPLES),
                        default=None, help="Target platform (default: host)")
    parser.add_argument("--output", default=None,
                        help="Payload output dir (default: the layout's "
                             "assembledDir, resolved against the repo root)")
    parser.add_argument("--runtime-dir", type=Path, default=None,
                        help="Use this existing portable-python tree")
    parser.add_argument("--runtime-archive", type=Path, default=None,
                        help="Use this install_only.tar.gz runtime archive")
    parser.add_argument("--skip-deps", action="store_true",
                        help="Do not install project dependencies (tests)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Do not run the payload import verification")
    args = parser.parse_args(argv)

    layout = load_layout()
    platform = args.platform or default_platform()
    if args.output:
        out = Path(args.output)
        out_dir = out if out.is_absolute() else (REPO_ROOT / out)
    else:
        out_dir = REPO_ROOT / layout["assembledDir"]
    try:
        manifest = assemble(
            platform,
            out_dir,
            runtime_dir=args.runtime_dir,
            runtime_archive=args.runtime_archive,
            skip_deps=args.skip_deps,
            skip_verify=args.skip_verify,
        )
    except AssemblerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Engine payload assembled at {out_dir} "
        f"(platform={manifest['platform']}, python={manifest['python_version']}, "
        f"runtime={manifest['runtime_source']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
