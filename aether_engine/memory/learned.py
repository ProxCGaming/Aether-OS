"""Epic 4, E4-10 (plan addendum omp #29, owner-approved 30 Oct
2026): the ``learn`` loop — explicit lessons in a curated document.

At task end the engine extracts *lessons* from the completed
exchange (working rules, corrections, and preferences about HOW to
work — distinct from the knowledge graph's facts about the user)
and appends them to ``~/.aether/learned.md``: deduped against what
is already there, secret-redacted with the house redactor (the
audit module's ``_scrub_string``, the same scrubber session export
uses), and capped so the document stays a briefing-sized artefact
rather than an ever-growing log. The supervisor's session briefing
then injects the lessons into the next session (see
``learned_briefing``), closing the loop the addendum promised.

The document is a file in the data home, NOT a backend store: it
is curated guidance that sits above whichever memory backend is
configured, so the facade's ``learn_lessons`` default serves every
backend and adapters do not override it. Writes are atomic (temp
file in the same directory + ``os.replace`` + fsync), matching the
engine's crash-safe file discipline; a crash mid-write leaves the
previous document intact.

Caps are module constants, in the style of the supervisor's
MEMORY_TOOL_RESULT_MAX_CHARS: MAX_LESSONS entries, each at most
MAX_LESSON_CHARS, the whole file at most MAX_LEARNED_CHARS. When a
cap bites, the OLDEST lessons are dropped — recent corrections
outrank stale ones.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger, _scrub_string
from aether_engine.providers.base import BaseProvider

_audit_logger = AuditLogger()

logger = logging.getLogger("aether_engine.memory.learned")

# Document paths live beside the memory databases in ~/.aether.
LEARNED_MD_PATH = Path.home() / ".aether" / "learned.md"

MAX_LESSONS = 100
MAX_LESSON_CHARS = 500
MAX_LEARNED_CHARS = 20_000
MAX_LESSONS_PER_TASK = 5
BRIEFING_MAX_CHARS = 2_000

_HEADER = (
    "# Learned lessons\n"
    "\n"
    "Explicit lessons Aether extracted at task end (addendum omp #29). "
    "Deduped, secret-redacted and capped; the oldest lessons are "
    "dropped when a cap is reached. Injected into the supervisor's "
    "briefing for the next session.\n"
    "\n"
)


def _normalise_lesson(text: str | None) -> str:
    """Dedupe key: whitespace-collapsed, case-folded (mirrors the
    KG's _normalise_kg_text, extended to inner whitespace so a
    re-worded copy of the same lesson still dedupes)."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _clean_lesson(text: str | None) -> str:
    """One lesson, safe to persist: single line, secret-scrubbed,
    length-capped. Empty string means 'not a lesson'."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = _scrub_string(cleaned)
    if len(cleaned) > MAX_LESSON_CHARS:
        cleaned = cleaned[:MAX_LESSON_CHARS].rstrip()
    return cleaned


def load_lessons(path: Path | None = None) -> list[str]:
    """The lessons currently in learned.md, oldest first.

    Lessons are the ``- `` bullet lines of the document; the header
    and any other prose are structure, not lessons. A missing file
    is an empty document, not an error.
    """
    target = path if path is not None else LEARNED_MD_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    lessons = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            lesson = line[2:].strip()
            if lesson:
                lessons.append(lesson)
    return lessons


def _render(lessons: list[str]) -> str:
    body = "".join(f"- {lesson}\n" for lesson in lessons)
    return _HEADER + body


def _enforce_caps(lessons: list[str]) -> list[str]:
    """Drop the oldest lessons until both caps hold."""
    capped = lessons[-MAX_LESSONS:] if len(lessons) > MAX_LESSONS else list(lessons)
    while capped and len(_render(capped)) > MAX_LEARNED_CHARS:
        capped.pop(0)
    return capped


def _write_lessons(lessons: list[str], path: Path) -> None:
    """Atomically replace the document (temp + fsync + os.replace):
    a crash leaves either the old or the new document, never a
    truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_render(lessons))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def record_lessons(lessons: list[str], task_id: str = "",
                   path: Path | None = None) -> dict[str, Any]:
    """Append new lessons to learned.md, deduped and capped.

    Returns {"added": [...], "skipped_duplicates": int,
    "total": int}. The file is only rewritten when something was
    actually added (a cap drop caused by additions rides along).
    """
    target = path if path is not None else LEARNED_MD_PATH
    existing = load_lessons(target)
    seen = {_normalise_lesson(lesson) for lesson in existing}
    added: list[str] = []
    skipped = 0
    for raw in lessons or []:
        cleaned = _clean_lesson(raw)
        if not cleaned:
            continue
        key = _normalise_lesson(cleaned)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        added.append(cleaned)
    if added:
        merged = _enforce_caps(existing + added)
        _write_lessons(merged, target)
        _audit_logger.log_event("LESSONS_RECORDED", {
            "task_id": task_id,
            "added": len(added),
            "duplicates_skipped": skipped,
            "total": len(merged),
        })
        logger.info("Recorded %d lesson(s) from task %s (%d total)",
                    len(added), task_id, len(merged))
        return {"added": added, "skipped_duplicates": skipped, "total": len(merged)}
    return {"added": [], "skipped_duplicates": skipped, "total": len(existing)}


async def extract_and_record_lessons(
    provider: BaseProvider,
    user_prompt: str,
    assistant_response: str,
    task_id: str,
) -> list[str]:
    """Extract explicit lessons from one completed exchange and
    record them in learned.md. Returns the lessons actually added.

    Mirrors knowledge_graph.extract_and_store_facts: same greeting /
    substance guards, same provider-stream shape, and best-effort by
    contract — any failure (provider down, bad JSON, disk error) is
    logged and returns what was added so far (usually []); lesson
    extraction must never fail a completed task.
    """
    # Skip pure short greetings with no substance (same guard as
    # fact extraction: nothing to learn from "hi" / "Noted!").
    pure_greetings = {"hi", "hello", "hey", "hii", "heyy", "sup", "yo"}
    if user_prompt.strip().lower() in pure_greetings and len(assistant_response) < 40:
        return []
    if len(user_prompt.strip()) < 3:
        return []

    system_prompt = """
You are a Lesson Extraction Engine.
Extract explicit LESSONS from the completed task below: durable working
rules the assistant should apply in FUTURE sessions — corrections the
user made, approaches that worked or failed, and stated preferences
about how work should be done. Lessons are about HOW to work, not facts
about the user (facts belong to the knowledge graph).
Return a JSON array of short standalone strings, each one lesson,
at most 5. Example: ["Always run the test suite before committing.",
"Prefer small, focused commits."]
If there is no durable lesson, return an empty array [].
Respond ONLY with a raw JSON array, no markdown code blocks, no explanations.
"""
    conversation = f"User: {user_prompt}\nAssistant: {assistant_response}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation},
    ]
    try:
        response_text = ""
        async for chunk in provider.call_stream(messages=messages):
            if chunk.text:
                response_text += chunk.text

        response_text = response_text.strip()
        response_text = response_text.removeprefix("```json")
        response_text = response_text.removesuffix("```")
        response_text = response_text.removeprefix("```")

        lessons_list = json.loads(response_text.strip())
        if not isinstance(lessons_list, list):
            return []
        candidates = [item for item in lessons_list if isinstance(item, str)]
        result = record_lessons(candidates[:MAX_LESSONS_PER_TASK], task_id=task_id)
        return result["added"]
    except json.JSONDecodeError:
        logger.warning("Lesson extraction returned invalid JSON. Raw output: %s",
                       response_text[:100])
        return []
    except Exception as e:  # noqa: BLE001 — best-effort by contract (see docstring)
        logger.warning(f"Lesson extraction failed: {e}")
        return []


def learned_briefing(max_chars: int = BRIEFING_MAX_CHARS) -> str:
    """The lessons rendered for the next session's briefing.

    Empty string when there are no lessons (the briefing simply has
    no lessons section). Capped at max_chars, cut at a lesson
    boundary with the same truncation-marker style the supervisor's
    memory tools use, so the briefing budget holds no matter how
    full learned.md is.
    """
    lessons = load_lessons()
    if not lessons:
        return ""
    header = "Lessons learned from earlier sessions (from learned.md — apply them when relevant):"
    lines = [header]
    used = len(header)
    for lesson in lessons:
        line = f"- {lesson}"
        if used + len(line) + 1 > max_chars:
            lines.append("...[lessons truncated to budget]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
