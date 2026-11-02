"""Launch the Aether desktop UI (Electron app in ``desktop_app/``).

The retired PySide6 UI (``aether_ui/``, launched here before Phase 10.3 D3)
is no longer used: it was deprecated in favour of the Electron/React app
(CHANGELOG [2026-09-20]; ADR 0018/0019) and its imports (PySide6, qasync,
Windows-only ``ctypes.wintypes``) do not even load on Linux. The live UI is
the Electron app in ``desktop_app/``; this script launches it the same way
the repo documents: ``npm run electron:dev`` (vite dev server + Electron).

The engine is a separate process (``run_engine.py``, 127.0.0.1:8000) and
the Electron main process reconnects to it on its own
(``desktop_app/main.cjs`` ``connectEngine`` retries every 3s), so the
engine does not need to be running before the UI starts — a warning is
printed if it is not reachable, nothing more.

Usage:
    python run_ui.py              # dev: vite + Electron (npm run electron:dev)
    python run_ui.py --prod       # build once, then launch Electron on dist/
    python run_ui.py --dry-run    # print the command that would run, exit 0
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DESKTOP_APP = REPO_ROOT / "desktop_app"
ENGINE_HOST = "127.0.0.1"
ENGINE_PORT = 8000


def _find_npm() -> str:
    npm = shutil.which("npm")
    if npm is None:
        sys.exit(
            "ERROR: 'npm' was not found on PATH. Install Node.js "
            "(https://nodejs.org) and retry."
        )
    return npm


def _check_desktop_app() -> None:
    if not (DESKTOP_APP / "package.json").is_file():
        sys.exit(f"ERROR: Electron app not found at {DESKTOP_APP} (no package.json).")
    if not (DESKTOP_APP / "node_modules").is_dir():
        sys.exit(
            "ERROR: desktop_app dependencies are not installed.\n"
            "Run 'npm ci' inside desktop_app/ first."
        )


def _warn_if_engine_down() -> None:
    try:
        with socket.create_connection((ENGINE_HOST, ENGINE_PORT), timeout=1.0):
            return
    except OSError:
        print(
            f"WARNING: engine not reachable at {ENGINE_HOST}:{ENGINE_PORT} "
            "(start it with 'python run_engine.py'). The UI will keep "
            "retrying the connection on its own.",
            file=sys.stderr,
        )


def _run(cmd: list[str]) -> int:
    # On Windows, npm is npm.cmd, which subprocess cannot exec directly
    # from a list; route through the shell with a properly quoted string.
    if os.name == "nt":
        return subprocess.run(
            subprocess.list2cmdline(cmd), cwd=DESKTOP_APP, shell=True, check=False
        ).returncode
    return subprocess.run(cmd, cwd=DESKTOP_APP, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch the Aether desktop UI (Electron app)."
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Build the React app once (npm run build) and launch Electron "
        "against the production bundle instead of the vite dev server.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command that would run (and the working directory) "
        "without launching anything.",
    )
    args = parser.parse_args(argv)

    npm = _find_npm()
    _check_desktop_app()

    if args.prod:
        build_cmd = [npm, "run", "build"]
        electron = DESKTOP_APP / "node_modules" / ".bin" / "electron"
        if electron.is_file():
            launch_cmd = [str(electron), "."]
        else:
            launch_cmd = [shutil.which("npx") or "npx", "electron", "."]
        steps = [build_cmd, launch_cmd]
    else:
        steps = [[npm, "run", "electron:dev"]]

    if args.dry_run:
        for cmd in steps:
            print(f"[dry-run] (cwd={DESKTOP_APP}) {' '.join(cmd)}")
        return 0

    _warn_if_engine_down()

    for cmd in steps:
        print(f"[run_ui] (cwd={DESKTOP_APP}) {' '.join(cmd)}", flush=True)
        rc = _run(cmd)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
