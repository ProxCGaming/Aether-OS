"""Filesystem read/search tools (Epic 2): read_file, find_files, search_text.

All READ_ONLY and containment-checked through ``tools/paths.py`` — the
same boundary pre-flight validation enforces for writes. Reads are
byte-faithful: the sha256 header is over the exact bytes on disk, line
splitting is display-only, so the hash an agent presents to
``edit_file`` (E2-03) always describes the file as it truly is.
"""

from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase
from pathlib import Path

from aether_engine.tools.paths import (
    content_hash,
    effective_roots,
    looks_binary,
    resolve_contained,
)
from aether_engine.tools.policy import is_sensitive_floor_path, path_rule_denies
from aether_engine.tools.registry import Tool, ToolRisk

#: Default and maximum line-window sizes for read_file.
READ_DEFAULT_LINES = 500
READ_MAX_LINES = 2000
#: Character budget for one read window (the registry does not truncate
#: in-process results, so the tool budgets itself — with a loud note).
READ_MAX_CHARS = 16000

#: Result/scan budgets for find_files / search_text — same doctrine as
#: READ_MAX_CHARS: cap loudly, never truncate silently.
FIND_MAX_RESULTS = 200
SEARCH_MAX_MATCHES = 100
SEARCH_MAX_FILES_SCANNED = 2000
#: Total character budget for search_text output — the PRD's "large-tree
#: run stays within output budget". Mirrors the Tool.max_output_chars
#: default (20000): the registry does not truncate in-process results,
#: so the tool budgets itself, clamping loudly like read_file does.
SEARCH_MAX_OUTPUT_CHARS = 20000


def _read_file_fn(
    path: str,
    offset: int | None = None,
    limit: int | None = None,
    workspace_roots: list | None = None,
) -> str:
    target = resolve_contained(path, workspace_roots)
    if not target.exists():
        return f"Error: file does not exist: {path}"
    if not target.is_file():
        return f"Error: not a file: {path}"
    data = target.read_bytes()
    if looks_binary(data):
        return f"Error: '{path}' looks like a binary file; read_file only reads text."
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: '{path}' is not valid UTF-8 text; read_file cannot display it."

    lines = text.splitlines()
    total = len(lines)
    start = max(1, offset or 1)
    window = min(limit or READ_DEFAULT_LINES, READ_MAX_LINES)
    end = min(total, start + window - 1)
    shown = lines[start - 1 : end] if total else []

    numbered = "\n".join(f"{n}: {line}" for n, line in enumerate(shown, start=start))
    truncated_note = ""
    if len(numbered) > READ_MAX_CHARS:
        numbered = numbered[:READ_MAX_CHARS]
        truncated_note = (
            f"\n[truncated at {READ_MAX_CHARS} characters — "
            "read a smaller window with offset/limit]"
        )
    header = (
        f"[read_file: {target} | sha256:{content_hash(data)} | "
        f"lines:{total} | showing {start}-{end}]"
    )
    body = numbered if shown else "(empty file)"
    return f"{header}\n{body}{truncated_note}"


def create_read_file_tool() -> Tool:
    return Tool(
        name="read_file",
        description=(
            "Read a text file inside the workspace. Returns the content with line "
            "numbers and a header carrying the file's sha256 hash — pass that hash "
            "to edit_file so edits fail safely if the file changed since you read it. "
            "Use offset (1-based line) and limit to read a window of a large file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, inside a workspace root."},
                "offset": {"type": "integer", "description": "First line to show (1-based, default 1)."},
                "limit": {"type": "integer", "description": "Maximum lines to show (default 500, max 2000)."},
            },
            "required": ["path"],
        },
        execute_fn=_read_file_fn,
        risk=ToolRisk.READ_ONLY,
        wants_workspace_roots=True,
    )


def _walk_files(base: Path, include_hidden: bool):
    """Yield files under ``base`` in deterministic (sorted) order.

    ``.git`` is pruned ALWAYS — version-control internals are never find
    or search material. Dot-directories and dotfiles are skipped unless
    ``include_hidden``. ``os.walk`` does not follow symlinked dirs, so a
    link pointing outside the workspace is never descended into.
    """
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if d != ".git" and (include_hidden or not d.startswith("."))
        )
        for name in sorted(filenames):
            if not include_hidden and name.startswith("."):
                continue
            yield Path(dirpath) / name


def _glob_match(pattern: str, rel_posix: str) -> bool:
    """Segment-wise glob match with pathlib-glob semantics (incl. ``**``).

    Written out instead of ``PurePath.match``: on Python 3.12 ``match``
    is right-anchored and treats ``**/`` as one-or-more directories, so
    ``**/*.py`` would miss top-level files — exactly the case agents
    hit first. Here ``**`` matches zero or more whole segments and
    every other segment must match in place (``fnmatchcase`` per
    segment, so ``*`` never crosses ``/``).
    """
    pat_parts = [p for p in pattern.replace("\\", "/").split("/") if p not in ("", ".")]
    return _match_parts(pat_parts, rel_posix.split("/"))


def _match_parts(pat_parts: list[str], path_parts: list[str]) -> bool:
    if not pat_parts:
        return not path_parts
    head, rest = pat_parts[0], pat_parts[1:]
    if head == "**":
        return any(_match_parts(rest, path_parts[i:]) for i in range(len(path_parts) + 1))
    if not path_parts:
        return False
    return fnmatchcase(path_parts[0], head) and _match_parts(rest, path_parts[1:])


def _display_path(candidate: Path, base: Path, single_base: bool) -> str:
    """Relative to the searched root when unambiguous, else absolute."""
    if single_base:
        return candidate.relative_to(base).as_posix()
    return str(candidate)


def _find_files_fn(
    pattern: str,
    path: str | None = None,
    include_hidden: bool = False,
    workspace_roots: list | None = None,
) -> str:
    if not pattern or not pattern.strip():
        return "Error: find_files needs a non-empty glob pattern."
    if path:
        base = resolve_contained(path, workspace_roots)
        if not base.exists():
            return f"Error: directory does not exist: {path}"
        if not base.is_dir():
            return f"Error: not a directory: {path}"
        bases = [base]
    else:
        bases = [r.resolve() for r in effective_roots(workspace_roots) if r.is_dir()]

    single = len(bases) == 1
    seen: set[Path] = set()
    found: list[str] = []
    for base in bases:
        for candidate in _walk_files(base, include_hidden):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _glob_match(pattern, candidate.relative_to(base).as_posix()):
                found.append(_display_path(candidate, base, single))
    found.sort()
    if not found:
        return f"No files matched pattern '{pattern}'."
    out = "\n".join(found[:FIND_MAX_RESULTS])
    if len(found) > FIND_MAX_RESULTS:
        out += f"\n[capped at {FIND_MAX_RESULTS} — narrow the pattern]"
    return out


def create_find_files_tool() -> Tool:
    return Tool(
        name="find_files",
        description=(
            "Find files by glob pattern inside the workspace: '**/*.py' matches Python "
            "files at any depth, 'src/*.ts' matches one folder only ('*' does not "
            "cross directories, '**' does). Optionally search under one directory "
            "with path. Hidden files and .git are skipped unless include_hidden is "
            f"set. Returns matching paths, capped at {FIND_MAX_RESULTS}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/*.ts'."},
                "path": {"type": "string", "description": "Optional directory inside a workspace root to search under (default: every workspace root)."},
                "include_hidden": {"type": "boolean", "description": "Also search dotfiles/dot-directories (default false; .git is always skipped)."},
            },
            "required": ["pattern"],
        },
        execute_fn=_find_files_fn,
        risk=ToolRisk.READ_ONLY,
        wants_workspace_roots=True,
    )


def _file_pattern_matches(file_pattern: str, name: str, rel_posix: str) -> bool:
    if "/" in file_pattern:
        return _glob_match(file_pattern, rel_posix)
    return fnmatchcase(name, file_pattern)


def _search_text_fn(
    query: str,
    path: str | None = None,
    include_hidden: bool = False,
    file_pattern: str | None = None,
    workspace_roots: list | None = None,
    path_rules: list | None = None,
) -> str:
    if not query:
        return "Error: search_text needs a non-empty query."
    try:
        regex = re.compile(query)
    except re.error as e:
        return f"Error: invalid regular expression '{query}': {e}"

    resolved_roots = [r.resolve() for r in effective_roots(workspace_roots)]
    # (file, display path) pairs — display is relative to the searched
    # base where feasible, the path as given for a single-file target.
    # E2-F08: candidates are a lazy iterator, never a materialised
    # list — the scan loop below stops pulling the moment the scan cap
    # trips, so a huge tree costs bounded enumeration work instead of
    # a full walk up front.
    # E2-F03: policy judged the call's starting path, and ONLY that
    # path. An explicit single-file target is that judged path itself
    # (a Deny rule blocks the call outright; the sensitive floor
    # forces its approval) — the read loop below trusts the judgement
    # for it. Everything the walk DISCOVERS was never judged, so each
    # discovered file is checked against the floor and the path-Deny
    # rules before it is opened.
    judged_target: Path | None = None
    if path:
        target = resolve_contained(path, workspace_roots)
        if not target.exists():
            return f"Error: path does not exist: {path}"
        if target.is_file():
            candidates = iter([(target, path)])
            judged_target = target
        else:
            candidates = (
                (f, f.relative_to(target).as_posix())
                for f in _walk_files(target, include_hidden)
            )
    else:
        single = len(resolved_roots) == 1

        def _all_root_candidates():
            for base in resolved_roots:
                if not base.is_dir():
                    continue
                for f in _walk_files(base, include_hidden):
                    yield (f, _display_path(f, base, single))

        candidates = _all_root_candidates()

    matches: list[str] = []
    out_chars = 0
    scanned = 0
    scan_capped = False
    match_capped = False
    output_capped = False
    for candidate, display in candidates:
        if file_pattern and not _file_pattern_matches(
            file_pattern, candidate.name, display
        ):
            continue
        if scanned >= SEARCH_MAX_FILES_SCANNED:
            scan_capped = True
            break
        # A symlinked file inside the workspace can point outside it;
        # os.walk never descends into one, but reading follows it — so
        # prove the RESOLVED file is contained before opening it.
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(r) for r in resolved_roots):
            continue
        # E2-F03: containment is the sandbox boundary, not the whole
        # policy. A discovered descendant under a sensitive-floor
        # prefix (~/.ssh, ~/.aether) or an explicit path-Deny rule is
        # skipped: policy never judged it, and a search cannot pause
        # per file, so exclusion is the fail-closed verdict.
        if resolved != judged_target and (
            is_sensitive_floor_path(resolved)
            or path_rule_denies(
                resolved, path_rules, tool_name="search_text", risk=ToolRisk.READ_ONLY
            )
        ):
            continue
        try:
            data = candidate.read_bytes()
        except OSError:
            continue
        scanned += 1
        if looks_binary(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if not regex.search(line):
                continue
            entry = f"{display}:{n}: {line}"
            if len(entry) > SEARCH_MAX_OUTPUT_CHARS:
                # E2-F08: one matching line (minified/generated files)
                # can exceed the whole output budget on its own —
                # clamp the line itself, loudly, as read_file clamps
                # its window.
                entry = entry[:SEARCH_MAX_OUTPUT_CHARS] + " [line truncated]"
            if matches and out_chars + len(entry) + 1 > SEARCH_MAX_OUTPUT_CHARS:
                output_capped = True
                break
            matches.append(entry)
            out_chars += len(entry) + 1
            if len(matches) >= SEARCH_MAX_MATCHES:
                match_capped = True
                break
        if match_capped or output_capped:
            break

    if not matches:
        out = f"No matches found for '{query}'."
    else:
        out = "\n".join(matches)
    if match_capped:
        out += f"\n[capped at {SEARCH_MAX_MATCHES} matching lines — narrow the query or path]"
    if output_capped:
        out += f"\n[output capped at {SEARCH_MAX_OUTPUT_CHARS} characters — narrow the query or path]"
    if scan_capped:
        out += f"\n[stopped after scanning {SEARCH_MAX_FILES_SCANNED} files — narrow the path]"
    return out


def create_search_text_tool() -> Tool:
    return Tool(
        name="search_text",
        description=(
            "Search file contents with a regular expression inside the workspace. "
            "Returns matches as 'path:line: text'. Search one file or directory "
            "with path, restrict to file types with file_pattern (e.g. '*.py'). "
            "Binary files are skipped; hidden files and .git are skipped unless "
            "include_hidden is set. Sensitive system locations and paths the "
            f"user has denied are never searched. Capped at {SEARCH_MAX_MATCHES} matching lines "
            f"and {SEARCH_MAX_OUTPUT_CHARS} output characters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Regular expression to search for."},
                "path": {"type": "string", "description": "Optional file or directory inside a workspace root to search (default: every workspace root)."},
                "include_hidden": {"type": "boolean", "description": "Also search dotfiles/dot-directories (default false; .git is always skipped)."},
                "file_pattern": {"type": "string", "description": "Optional glob filter for file names, e.g. '*.py'."},
            },
            "required": ["query"],
        },
        execute_fn=_search_text_fn,
        risk=ToolRisk.READ_ONLY,
        wants_workspace_roots=True,
        wants_path_rules=True,
    )


def filesystem_read_tools() -> list[Tool]:
    """The READ_ONLY filesystem suite: read + find + search (E2-01/E2-02)."""
    return [create_read_file_tool(), create_find_files_tool(), create_search_text_tool()]
