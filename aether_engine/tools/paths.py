"""Shared path containment + content helpers for filesystem tools (Epic 2).

One implementation of the containment rule every path-taking tool must
obey — the same resolve-then-check rule pre-flight validation uses
(``validation/pre_flight.py``): a target is allowed only when its fully
resolved path (symlinks followed) sits inside one of the workspace
roots. Tools raise :class:`PathContainmentError`; the registry wraps
execution errors into ``ToolExecutionError`` with the message intact.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


class PathContainmentError(ValueError):
    """A tool path resolved outside every permitted workspace root."""


def effective_roots(workspace_roots: list | None) -> list[Path]:
    """The roots a tool call may touch.

    Falls back to ``~/AetherWorkspace`` when the run configured none —
    the same default pre-flight validation applies, so a tool and its
    validation can never disagree about the boundary.
    """
    if workspace_roots:
        return [Path(r) for r in workspace_roots]
    return [Path.home() / "AetherWorkspace"]


def resolve_contained(path_str: str, workspace_roots: list | None) -> Path:
    """Resolve ``path_str`` and prove it sits inside a permitted root.

    ``Path.resolve()`` follows symlinks, so a symlink inside the
    workspace that points outside is caught here, not after the fact.
    """
    if not path_str:
        raise PathContainmentError("Path validation failed: empty path.")
    target = Path(path_str).resolve()
    for root in effective_roots(workspace_roots):
        if target.is_relative_to(root.resolve()):
            return target
    raise PathContainmentError(
        f"Path validation failed: '{path_str}' is outside the permitted workspace roots."
    )


# S2-N1: argument keys harvested as paths beyond the canonical
# "path". Built-in tools all use "path", but MCP servers and some
# artifact tools name the same thing differently — a floor or grant
# that only reads "path" judges those calls path-less and lets them
# past rules every other tool is held to.
_PATH_ARG_KEYS = (
    "path",
    "file_path",
    "directory",
    "dir",
    "folder",
    "root",
    "destination",
    "target_path",
)


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.startswith("~")


def candidate_paths(args: dict | None) -> list[Path]:
    """Resolved paths a tool call would touch, from its arguments.

    Shared by policy (E2-06 floor + path rules) and the approval cache
    (E2-09 grant matching) so every layer judges the same paths.

    S2-N1: besides ``path`` (always taken), the common alternate keys
    in ``_PATH_ARG_KEYS`` are harvested when their value looks like a
    path (contains a separator or starts with ``~``), and an
    execute_shell ``command`` contributes its absolute-path tokens
    (including ``--flag=/abs/path`` forms). This is a heuristic with
    known limits, by design: an unlisted key, a bare relative filename
    under an alternate key, and relative tokens inside a shell command
    stay invisible here — the execution-time containment in the tools
    themselves (``resolve_contained``) remains the hard boundary, and
    these candidates exist so the policy floor and grant matching see
    the paths the common shapes actually carry.
    """
    if not args:
        return []
    found: list[Path] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        resolved = str(Path(raw).expanduser().resolve())
        if resolved not in seen:
            seen.add(resolved)
            found.append(Path(resolved))

    for key in _PATH_ARG_KEYS:
        raw = args.get(key)
        if isinstance(raw, str) and raw and (key == "path" or _looks_like_path(raw)):
            _add(raw)
    command = args.get("command")
    if isinstance(command, str) and command:
        for token in re.split(r"[\s\"'<>|;&]+", command):
            candidate = token.strip("`").rstrip(".,;:").rsplit("=", 1)[-1]
            if candidate.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", candidate):
                _add(candidate)
    return found


def content_hash(data: bytes) -> str:
    """Full sha256 of the exact bytes — the anchor edits present (E2-03)."""
    return hashlib.sha256(data).hexdigest()


def looks_binary(data: bytes) -> bool:
    """Cheap binary sniff: a NUL byte in the first 8 KB."""
    return b"\x00" in data[:8192]
