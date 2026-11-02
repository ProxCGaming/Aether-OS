"""Dev-server detection for the canvas project preview (Epic 8, E8-2.2).

Probes the PRD candidate ports (5173, 3000, 8000, 8080) on localhost for a
live HTTP server and identifies the project framework from ``package.json``
(dependencies first, then the ``scripts`` commands). The result feeds the
``DEV_SERVER_DETECT_RESPONSE`` payload::

    {"running": bool, "port": int | None, "url": str | None,
     "framework": str | None}

A port only counts as a dev server when it answers an HTTP request — a raw
TCP listener (database, debugger, …) must never be reported as running,
because the sandboxed preview iframe could not load it. ``framework`` is
project metadata from ``package.json`` and is reported whether or not a
server is currently running, so the UI can say what would be started.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# PRD candidate ports, in probe order.
DEFAULT_PORTS: tuple[int, ...] = (5173, 3000, 8000, 8080)

_PROBE_TIMEOUT_SECONDS = 0.75

# (package/script token, display name), checked in order — the more
# specific wrappers (Next.js, Nuxt, SvelteKit) come before the libraries
# they build on.
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (
    ("next", "Next.js"),
    ("nuxt", "Nuxt"),
    ("@angular/cli", "Angular"),
    ("vite", "Vite"),
    ("react-scripts", "Create React App"),
    ("@sveltejs/kit", "SvelteKit"),
    ("svelte", "Svelte"),
    ("vue", "Vue"),
)


def _default_probe(port: int) -> bool:
    """True when 127.0.0.1:<port> answers an HTTP request.

    TCP-connect first, then a minimal ``GET /`` — only an ``HTTP/``
    status line counts. Any socket error means "not a dev server".
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_PROBE_TIMEOUT_SECONDS) as sock:
            sock.settimeout(_PROBE_TIMEOUT_SECONDS)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            return sock.recv(64).startswith(b"HTTP/")
    except OSError:
        return False


def _candidate_dirs(workspace_root: str | Path | None, project_path: str | Path | None) -> list[Path]:
    """Directories to look for package.json in, most specific first."""
    dirs: list[Path] = []
    for raw in (project_path, workspace_root):
        if not raw:
            continue
        path = Path(raw)
        dirs.append(path if path.is_dir() or not path.suffix else path.parent)
    return dirs


def parse_package_json(project_path: str | Path | None) -> dict[str, Any] | None:
    """Parse ``package.json`` in (or above, for a file path) a directory.

    Returns the parsed dict, or None when the file is missing or invalid.
    """
    if not project_path:
        return None
    path = Path(project_path)
    directory = path if path.is_dir() or not path.suffix else path.parent
    return _read_package_json(directory)


def _read_package_json(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_package_json(
    workspace_root: str | Path | None,
    project_path: str | Path | None,
) -> dict[str, Any] | None:
    for directory in _candidate_dirs(workspace_root, project_path):
        package = _read_package_json(directory)
        if package is not None:
            return package
    return None


def detect_framework(package: dict[str, Any] | None) -> str | None:
    """Identify the framework from package.json dependencies or scripts."""
    if not package:
        return None

    dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        section = package.get(key)
        if isinstance(section, dict):
            dependencies.update(section)
    for token, name in _FRAMEWORK_MARKERS:
        if token in dependencies:
            return name

    scripts = package.get("scripts")
    if isinstance(scripts, dict):
        commands = " ".join(str(value) for value in scripts.values())
        for token, name in _FRAMEWORK_MARKERS:
            if token in commands:
                return name
    return None


def detect_dev_server(
    workspace_root: str | Path | None = None,
    project_path: str | Path | None = None,
    *,
    ports: Iterable[int] = DEFAULT_PORTS,
    probe: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Probe candidate ports for a running dev server.

    Returns the DEV_SERVER_DETECT_RESPONSE payload. The first port (in
    candidate order) whose probe succeeds wins.
    """
    probe_fn = probe if probe is not None else _default_probe
    framework = detect_framework(_load_package_json(workspace_root, project_path))

    for port in ports:
        try:
            running = bool(probe_fn(port))
        except Exception:  # noqa: BLE001 — a failing probe means "not this port", never a crash
            running = False
        if running:
            return {
                "running": True,
                "port": port,
                "url": f"http://localhost:{port}",
                "framework": framework,
            }
    return {"running": False, "port": None, "url": None, "framework": framework}
