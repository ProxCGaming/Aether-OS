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

Dependencies are installed INTO the payload runtime from the
project's ONE source of truth, ``uv.lock`` — never from pyproject
version ranges. The assembler exports the lock for the target
platform (``uv export --locked --format requirements-txt``, hashes
included) and installs that file with
``uv pip install --python <payload python> --system``, so the
payload carries exactly the versions dev and CI tested, byte-for-
byte hash-verified. (Before 15 Nov 2026 this step installed the
pyproject ranges fresh; that drift is how an installer shipped
litellm 1.108.0 + mcp 1.30.0 — a pair whose tool-carrying calls all
crash — while the lock held a different pair.) Platform markers
(e.g. pywin32) resolve against the target platform triple. Assemble
on the target platform (the release workflow's matrix does exactly
that): wheels are platform-specific.

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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
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

# The download is the script's one network step: bounded like every
# subprocess step (60s/300s/1800s below) so a stalled connection
# fails the build instead of hanging it forever (D4-03).
DOWNLOAD_TIMEOUT_SECONDS = 300

# SHA-256 of the pinned release asset per platform, from
# python-build-standalone's published checksums for RUNTIME_PIN.
# The downloaded bytes become the interpreter inside every shipped
# installer, so when a digest is pinned here the download is
# verified against it before anything is extracted (D4-03).
# Filled 15 Nov 2026 from the 20261026 release's own SHA256SUMS
# asset (the release's checksum listing — never from downloaded
# bytes). Fill an entry in the same commit that bumps RUNTIME_PIN.
RUNTIME_SHA256: dict[str, str] = {
    "win32": "590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5",
    "linux": "6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de",
    "darwin": "a962e47df522889eaa7ff0d6a56203f1079f68aabb65ffdbe8455f362bc769f3",
}

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
    digest = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp, \
            open(dest_archive, "wb") as fh:
        while chunk := resp.read(1024 * 1024):
            digest.update(chunk)
            fh.write(chunk)
    expected = RUNTIME_SHA256.get(platform)
    if expected is not None and digest.hexdigest() != expected.lower():
        raise AssemblerError(
            f"Runtime download for {platform} failed its sha256 checksum check: "
            f"expected {expected.lower()}, got {digest.hexdigest()}. "
            "Refusing to extract or ship unverified bytes."
        )
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


def export_locked_requirements(
    platform: str,
    dest: Path,
    repo_root: Path = REPO_ROOT,
    runner=subprocess.run,
) -> str:
    """Export ``uv.lock`` to a hashed requirements file for a platform.

    uv.lock is the single source of truth for every Python version
    the app runs with; this export is the ONLY dependency input the
    payload ever installs from. ``--locked`` makes uv refuse when
    the lock disagrees with pyproject.toml, and the export carries
    each package's sha256, which the install step then verifies.
    Platform-specific packages (e.g. pywin32) travel as environment
    markers in the export and are selected by the install step
    against the payload interpreter — which is why assembly must
    run on the target platform. The ``platform`` argument is kept
    for call-site clarity and future per-platform exports.
    Returns the sha256 of the exported file (recorded in the
    payload manifest, so an assembled payload names the exact
    dependency set inside it).
    """
    uv = shutil.which("uv")
    if uv is None:
        raise AssemblerError(
            "The dependency step needs the `uv` binary on PATH "
            "(the project's own toolchain — see .github/workflows/ci.yml)"
        )
    if not (repo_root / "uv.lock").is_file():
        raise AssemblerError(f"uv.lock not found at {repo_root / 'uv.lock'}")
    cmd = [
        uv, "export",
        "--format", "requirements-txt",
        "--locked",
        "--no-emit-project",
        "--no-dev",
        "--output-file", str(dest),
    ]
    proc = runner(cmd, capture_output=True, text=True, timeout=300, cwd=repo_root)
    if proc.returncode != 0 or not dest.is_file():
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise AssemblerError(f"Exporting uv.lock for the payload failed:\n{tail}")
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def install_dependencies(
    layout: dict,
    platform: str,
    out_dir: Path,
    repo_root: Path = REPO_ROOT,
    runner=subprocess.run,
) -> str:
    """Install the locked dependency set into the payload runtime.

    Returns the sha256 of the exported requirements file that was
    installed (see export_locked_requirements).
    """
    uv = shutil.which("uv")
    if uv is None:
        raise AssemblerError(
            "The dependency step needs the `uv` binary on PATH "
            "(the project's own toolchain — see .github/workflows/ci.yml)"
        )
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    with tempfile.TemporaryDirectory(prefix="aether-deps-") as tmp:
        req = Path(tmp) / "requirements.txt"
        req_sha256 = export_locked_requirements(
            platform, req, repo_root=repo_root, runner=runner,
        )
        cmd = [
            uv, "pip", "install",
            "--python", str(python_exe),
            "--system",
            "--requirement", str(req),
        ]
        print("Installing the uv.lock dependency set into the payload runtime...")
        proc = runner(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise AssemblerError(f"Dependency installation failed:\n{tail}")
    return req_sha256


# The litellm smoke below exists because of a shipped failure
# (15 Nov 2026): litellm's tool-carrying completion path imports
# its MCP handler chain, and a litellm/mcp pair that disagrees
# about `mcp.types.CacheableResult` makes EVERY agent task fail
# with a misleading "network/service error" — while plain imports
# and tool-less calls succeed, so the engine-import check alone
# shipped the broken pair. A mock-response call with a tool runs
# the exact import chain with no network and no API key.
LITELLM_TOOLS_SMOKE = (
    "import asyncio, litellm\n"
    "TOOLS = [{'type': 'function', 'function': {'name': 'smoke', "
    "'description': 'smoke', 'parameters': {'type': 'object', "
    "'properties': {}}}}]\n"
    "async def _main():\n"
    "    await litellm.acompletion(\n"
    "        model='gemini/gemini-2.5-flash',\n"
    "        messages=[{'role': 'user', 'content': 'smoke'}],\n"
    "        mock_response='ok', tools=TOOLS)\n"
    "asyncio.run(_main())\n"
    "print('payload litellm tools ok')\n"
)


def verify_payload(
    layout: dict,
    platform: str,
    out_dir: Path,
    runner=subprocess.run,
) -> None:
    """The payload interpreter must import the engine entry point
    AND run a tool-carrying litellm call (see LITELLM_TOOLS_SMOKE)."""
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    app_dir = out_dir / layout["appDir"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(app_dir)
    checks = [
        (
            "engine import",
            (
                "import aether_engine.main, aether_common, aether_worker; "
                "print('payload import ok')"
            ),
            "payload import ok",
        ),
        ("litellm tools smoke", LITELLM_TOOLS_SMOKE, "payload litellm tools ok"),
    ]
    with tempfile.TemporaryDirectory(prefix="aether-verify-") as cwd:
        for label, code, marker in checks:
            proc = runner(
                [str(python_exe), "-c", code],
                capture_output=True, text=True, timeout=300, cwd=cwd, env=env,
            )
            if proc.returncode != 0 or marker not in proc.stdout:
                tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
                raise AssemblerError(
                    f"Payload verification ({label}) failed:\n{tail}"
                )


def write_manifest(
    layout: dict,
    platform: str,
    out_dir: Path,
    runtime_source: str,
    python_version: str,
    requirements_sha256: str | None = None,
    uv_lock_sha256: str | None = None,
) -> dict:
    manifest = {
        "format": 1,
        "platform": platform,
        "python_version": python_version,
        "runtime_source": runtime_source,
        # Dependency provenance: the payload's packages came from
        # this exact uv.lock (sha256 of the lock file) via the
        # hashed export named here — an installed app can be
        # checked against the repo's lock byte-for-byte.
        "uv_lock_sha256": uv_lock_sha256,
        "requirements_sha256": requirements_sha256,
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
        # D4-14: the output tree is deleted before assembly. Refuse
        # when the existing tree does not look like a payload this
        # script made — a mistyped --output must never recursively
        # delete an unrelated tree. A previous payload is recognised
        # by its manifest (or by carrying the layout's assembled-dir
        # name); an empty directory holds nothing to lose.
        has_entries = any(out_dir.iterdir())
        looks_like_payload = (
            (out_dir / layout["manifestFile"]).is_file()
            or out_dir.name == Path(layout["assembledDir"]).name
        )
        if has_entries and not looks_like_payload:
            raise AssemblerError(
                f"refusing to delete {out_dir}: it exists, is not empty, and "
                f"carries no {layout['manifestFile']} — it does not look like "
                "an assembled engine payload. Move it aside or pass a "
                "different --output."
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    runtime_source = acquire_runtime(
        layout, platform, out_dir,
        runtime_dir=runtime_dir, runtime_archive=runtime_archive,
    )
    python_exe = out_dir / runtime_exe_rel(layout, platform)
    python_version = check_runtime_version(python_exe, runner=runner)
    copy_app_packages(layout, out_dir, repo_root=repo_root)
    requirements_sha256 = None
    if not skip_deps:
        requirements_sha256 = install_dependencies(
            layout, platform, out_dir, repo_root=repo_root, runner=runner,
        )
    uv_lock = repo_root / "uv.lock"
    uv_lock_sha256 = (
        hashlib.sha256(uv_lock.read_bytes()).hexdigest() if uv_lock.is_file() else None
    )
    manifest = write_manifest(
        layout, platform, out_dir, runtime_source, python_version,
        requirements_sha256=requirements_sha256,
        uv_lock_sha256=uv_lock_sha256,
    )
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
