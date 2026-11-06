"""The shared step-to-message boundary rule for rewind and fork.

A task begins at the session's last user message:

* step 0 retains the history *before* that message and discards the
  task's messages;
* an intermediate step retains history through that message and
  discards what follows it;
* the final step retains the whole history and discards nothing.

The manifest (what a picker displays), the rewind service (what a
rewind discards), and fork (what a copied session retains) are three
views of this one rule. They all derive their answers here so a
future correction cannot update one surface and leave the others
promising a different boundary.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StepBoundary:
    """One step's boundary over an ordered message transcript."""

    anchored: bool
    cut_message_id: str | None
    discarded_rowids: tuple[int, ...]
    is_empty: bool


def _row_field(row: Any, field: str) -> Any:
    """Read ``field`` from a mapping or an ``sqlite3.Row``."""
    try:
        return row[field]
    except (IndexError, KeyError, TypeError):
        return getattr(row, field, None)


def step_boundary(
    rows: Iterable[Any], step_index: int, final_step: int,
) -> StepBoundary:
    """Resolve ``step_index`` against ordered transcript ``rows``.

    Rows must expose ``rowid``, ``id``, and ``role`` fields. When the
    transcript has no user message, the task cannot be anchored and
    the result is explicitly unanchored rather than guessed.
    """
    ordered = list(rows)
    start_pos = None
    for pos, row in enumerate(ordered):
        if _row_field(row, "role") == "user":
            start_pos = pos

    if start_pos is None:
        return StepBoundary(
            anchored=False,
            cut_message_id=None,
            discarded_rowids=(),
            is_empty=False,
        )

    rowids = tuple(_row_field(row, "rowid") for row in ordered)
    if step_index == 0:
        cut_pos = start_pos - 1
        return StepBoundary(
            anchored=True,
            cut_message_id=(
                _row_field(ordered[cut_pos], "id") if cut_pos >= 0 else None
            ),
            discarded_rowids=rowids[start_pos:],
            is_empty=cut_pos < 0,
        )
    if step_index < final_step:
        return StepBoundary(
            anchored=True,
            cut_message_id=_row_field(ordered[start_pos], "id"),
            discarded_rowids=rowids[start_pos + 1:],
            is_empty=False,
        )
    return StepBoundary(
        anchored=True,
        cut_message_id=_row_field(ordered[-1], "id"),
        discarded_rowids=(),
        is_empty=False,
    )
