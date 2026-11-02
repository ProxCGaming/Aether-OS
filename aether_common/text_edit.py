"""Anchored text edits, shared by the engine and the worker (Epic 2, E2-03).

Pure stdlib, no imports from ``aether_engine``: the worker deliberately
avoids importing the engine package (its ``__init__`` pulls in the whole
app), and ``aether_common`` is already on the worker's import path.

The contract:
- Every edit's ``old_text`` must occur EXACTLY ONCE in the current
  content. Zero occurrences (stale or mistyped anchor) and multiple
  occurrences (ambiguous anchor) both fail loudly — nothing is ever
  partially applied: all edits validate against the evolving content
  in memory first, and the caller writes only on total success.
- Callers verify the file's sha256 against the hash the agent read
  (``content_hash``) before applying, so a file that changed since the
  read is never edited blind.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from pathlib import Path

#: Cap on diff lines embedded in an approval payload.
MAX_DIFF_LINES = 200


class TextEditError(ValueError):
    """An anchored edit cannot be applied exactly as specified."""


def content_hash(data: bytes) -> str:
    """Full sha256 of the exact bytes (same value read_file reports)."""
    return hashlib.sha256(data).hexdigest()


def apply_text_edits(text: str, edits: list[dict]) -> str:
    """Apply ``[{old_text, new_text}, ...]`` in order, or raise.

    Each anchor is checked against the content as modified by the
    preceding edits, so overlapping edits fail instead of corrupting.
    """
    if not edits:
        raise TextEditError("edit_file requires at least one edit.")
    result = text
    for index, edit in enumerate(edits, start=1):
        old = edit.get("old_text") if isinstance(edit, dict) else None
        new = edit.get("new_text") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not isinstance(new, str) or old == "":
            raise TextEditError(
                f"Edit {index} is malformed: each edit needs a non-empty 'old_text' and a 'new_text' string."
            )
        occurrences = result.count(old)
        if occurrences == 0:
            raise TextEditError(
                f"Edit {index} failed: the anchor text was not found in the file. "
                "Re-read the file and anchor to its current content."
            )
        if occurrences > 1:
            raise TextEditError(
                f"Edit {index} failed: the anchor text occurs {occurrences} times — it must be unique. "
                "Include more surrounding context in old_text."
            )
        result = result.replace(old, new, 1)
    return result


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via a temp file in the same directory + ``os.replace``.

    An interrupted edit leaves either the old file or the new one —
    never a half-written file.
    """
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def make_unified_diff(old: str, new: str, path: str) -> str:
    """A bounded unified diff for the approval preview."""
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"… [diff truncated at {MAX_DIFF_LINES} lines]"]
    return "\n".join(lines)
