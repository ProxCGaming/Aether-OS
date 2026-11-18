"""Shared text signals for Aether's routing layer.

Single home for the keyword sets and raw text measurements that the
routing consumers — policy, model_routing, and intent — use to decide
what a task looks like.  **This module supplies measurements, not
decisions.**  Per-consumer thresholds (intent's 1200 / 500 / 80 chars;
model_routing's 1500 / 140) stay with the consumers; only the raw
numbers are computed here.

Also the home of ``infer_task_class``, moved unchanged from
``routing/task_class_router.py`` (which is now a thin compatibility
shim re-exporting it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Keyword sets (single definitions — no duplicates anywhere in the engine)
# ---------------------------------------------------------------------------

#: 17 code-domain terms.  ``def `` and ``class `` include a trailing
#: space so that ``"I am defined"`` does not trigger the code path.
CODE_KEYWORDS: frozenset[str] = frozenset({
    "code", "sql", "html", "css", "function", "def ", "class ", "bug",
    "refactor", "algorithm", "script", "regex", "api", "endpoint",
    "syntax", "compile", "traceback",
})

#: 10 reasoning phrases.
REASONING_KEYWORDS: frozenset[str] = frozenset({
    "step by step", "deep think", "reasoning", "prove", "math proof",
    "derivation", "complex analysis", "solve riddle", "logic puzzle",
    "benchmark comparison",
})

# ---------------------------------------------------------------------------
# Step-marker regexes — TWO distinct measurements, kept distinct on purpose
# ---------------------------------------------------------------------------

#: Loose markers: any ``N.`` / ``N)`` line start (any digit count, no
#: trailing content required) or a ``step N`` phrase.  This is the count
#: ``model_routing._complexity_need`` has always used.
_STEP_MARKERS_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|step\s+\d+\b)", re.IGNORECASE)

#: Strict numbered-list markers: 1–2 digits, ``.`` or ``)``, then
#: whitespace and content on the same line.  This is the count
#: ``intent.work_heuristic_reason`` has always used.  It deliberately
#: does NOT match ``step N`` phrases, bare ``1.`` lines, or 3+-digit
#: numbers (e.g. a line starting ``2026.``).
_NUMBERED_STEP_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s+\S")

# ---------------------------------------------------------------------------
# text_signals — raw measurements, no thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSignals:
    """Raw measurements of a text string.

    Consumers apply their own thresholds to these values:
    ``wants_code`` and ``wants_reasoning`` are boolean summaries of the
    keyword scans; ``step_count`` is the loose step-marker count (see
    ``_STEP_MARKERS_RE``); ``numbered_step_count`` is the strict
    numbered-list count (see ``_NUMBERED_STEP_RE``) — the two counts
    are different measurements for different consumers and are never
    substituted for each other; ``char_length`` is the raw character
    count.  No threshold comparisons are made here.
    """

    wants_code: bool
    wants_reasoning: bool
    step_count: int
    numbered_step_count: int
    char_length: int


def text_signals(text: str) -> TextSignals:
    """Return the raw measurements of *text* that routing consumers need.

    Args:
        text: The prompt or task text to inspect.

    Returns:
        :class:`TextSignals` with keyword flags, step-marker count, and
        character length.  The ``wants_code`` flag is also ``True`` when
        the text contains a triple-backtick code fence (```).
    """
    lower = text.lower()
    wants_code = any(kw in lower for kw in CODE_KEYWORDS) or ("```" in text)
    wants_reasoning = any(kw in lower for kw in REASONING_KEYWORDS)
    step_count = len(_STEP_MARKERS_RE.findall(text))
    numbered_step_count = len(_NUMBERED_STEP_RE.findall(text))
    return TextSignals(
        wants_code=wants_code,
        wants_reasoning=wants_reasoning,
        step_count=step_count,
        numbered_step_count=numbered_step_count,
        char_length=len(text),
    )


# ---------------------------------------------------------------------------
# infer_task_class — moved from routing/task_class_router.py unchanged
# ---------------------------------------------------------------------------

#: Heavy-execution shell-command patterns.
_HEAVY_SHELL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bgcc\b"),
    re.compile(r"\bg\+\+\b"),
    re.compile(r"\bcargo\s+(build|run|test|check)\b"),
    re.compile(r"\bnpm\s+(run\s+build|install)\b"),
    re.compile(r"\byarn\s+(build|install)\b"),
    re.compile(r"\bmsbuild\b"),
    re.compile(r"\bcmake\b"),
    re.compile(r"\bmake\b"),
    re.compile(r"\bdocker\s+(build|run)\b"),
    re.compile(r"\bpython\s+[\w\/\\]*\.py\b"),
]


def infer_task_class(tool_name: str, args: dict[str, Any]) -> str:
    """Infer the task class (instant, quick, standard, heavy) from a tool call.

    Heuristic rules (unchanged from the original ``task_class_router``):

    - ``delete_file``  → ``"instant"``
    - ``write_file``   → ``"quick"``
    - ``execute_shell`` → ``"heavy"`` for compile / build / run-script
      patterns; ``"standard"`` otherwise.
    - Unrecognised tool → ``"standard"``

    Currently has no production callers; ``routing/task_class_router.py``
    is a compatibility shim that re-exports it for the historical import path.
    """
    if tool_name == "delete_file":
        return "instant"

    if tool_name == "write_file":
        return "quick"

    if tool_name == "execute_shell":
        command = str(args.get("command", "")).strip().lower()
        for pat in _HEAVY_SHELL_PATTERNS:
            if pat.search(command):
                return "heavy"
        return "standard"

    return "standard"
