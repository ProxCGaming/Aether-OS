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


def candidate_paths(args: dict | None) -> list[Path]:
    """Resolved paths a tool call would touch, from its arguments.

    Shared by policy (E2-06 floor + path rules) and the approval cache
    (E2-09 grant matching) so every layer judges the same paths.
    """
    if not args:
        return []
    raw = args.get("path")
    if isinstance(raw, str) and raw:
        return [Path(raw).expanduser().resolve()]
    return []


def content_hash(data: bytes) -> str:
    """Full sha256 of the exact bytes — the anchor edits present (E2-03)."""
    return hashlib.sha256(data).hexdigest()


def looks_binary(data: bytes) -> bool:
    """Cheap binary sniff: a NUL byte in the first 8 KB."""
    return b"\x00" in data[:8192]
