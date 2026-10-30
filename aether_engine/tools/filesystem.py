"""Filesystem read/search tools (Epic 2): read_file, glob, grep.

All READ_ONLY and containment-checked through ``tools/paths.py`` — the
same boundary pre-flight validation enforces for writes. Reads are
byte-faithful: the sha256 header is over the exact bytes on disk, line
splitting is display-only, so the hash an agent presents to
``edit_file`` (E2-03) always describes the file as it truly is.
"""

from __future__ import annotations

from aether_engine.tools.paths import content_hash, looks_binary, resolve_contained
from aether_engine.tools.registry import Tool, ToolRisk

#: Default and maximum line-window sizes for read_file.
READ_DEFAULT_LINES = 500
READ_MAX_LINES = 2000
#: Character budget for one read window (the registry does not truncate
#: in-process results, so the tool budgets itself — with a loud note).
READ_MAX_CHARS = 16000


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


def filesystem_read_tools() -> list[Tool]:
    """The READ_ONLY filesystem suite (grows with E2-02)."""
    return [create_read_file_tool()]
