"""S8: conversational forget — intent path, cascading delete,
tombstone undo (master-build plan §4 S8, R12).

Before this slice, forgetting existed only in Settings → Memory
Management (edit/delete by ID). Saying "forget that" in chat at
best *added* a fact about wanting to forget, and a management-plane
delete did not cascade: the same content stayed in episodes,
``learned.md`` and the curated document. This module is the chat
path, modelled on the calendar layer's proven pattern (C2): the
ENGINE detects the intent deterministically (regex, no LLM),
resolves the referent to concrete records, deletes them itself —
the model is never trusted to delete — and returns a system note
so the reply announces exactly what happened, with an undo path.

Design rules:

* **Literal referent resolution.** Candidates are found by
  text/entity matching over the facade's stored records — never by
  embedding similarity. Recall (S6) may be fuzzy; DELETION must
  not be: a wrong semantic hit would erase the wrong memory. The
  note lists every deleted item individually, so a wrong literal
  hit is visible and undoable, never silent.
* **Cascade.** One forget removes the matched content from every
  memory surface it lives on: KG facts (whose S6 embedding rows
  cascade with them), episodes (FTS row + thinking + embedding,
  via the store's own delete), ``learned.md`` lesson lines, and
  the curated document — regenerated from the live stores after
  the delete, so a forgotten fact cannot keep resurfacing through
  the S5 assembler's curated leg while the weekly pass is pending.
* **Tombstone undo.** Everything deleted is snapshotted first
  (full stored records: ids, provenance, confidence, timestamps)
  into a bounded tombstone store; "undo forget" restores the most
  recent unconsumed tombstone and marks it consumed. Embedding
  vectors are NOT snapshotted — they are derived data (S6),
  rebuilt by the stores' backfills after a restore. Bounds:
  at most ``MAX_TOMBSTONES`` tombstones, an undo window of
  ``TOMBSTONE_TTL_SECONDS`` (30 days), at most
  ``MAX_FORGET_ITEMS`` records per operation. Undo is a safety
  net, not a second memory.
* **Honest degradation.** The service runs against any facade
  backend for finding + deleting (and lessons + curated regen,
  which are backend-independent). Snapshot/restore with original
  ids is a native-store capability; on a backend without it the
  deletion still happens but the result says plainly that undo is
  unsupported there — never a fake promise of restorability.
* **No bulk wipe from chat.** "Forget everything" (no topic) is
  NOT executed: the note directs the user to Settings → Memory
  Management for full clears. A topic-scoped request deletes only
  what the finder actually matched, listed item by item.
* **Sessions are out of scope.** Chat HISTORY (session
  transcripts) is not memory-store content: forget never rewrites
  or deletes session messages — that is the rewind feature's job.
  The session store keeps no search index over memory content
  (its search covers its own transcripts), so there is no session
  index leg to scrub; what forget scrubs is the memory-derived
  content listed under Cascade above.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger
from aether_engine.memory import learned

_audit_logger = AuditLogger()

logger = logging.getLogger("aether_engine.memory.forget")

# The tombstone store lives beside the memory DBs in the data home.
TOMBSTONES_PATH = Path.home() / ".aether" / "forget_tombstones.json"

MAX_TOMBSTONES = 20
TOMBSTONE_TTL_SECONDS = 30 * 24 * 3600  # the undo window: 30 days
MAX_FORGET_ITEMS = 50  # per-operation cap across facts + episodes

_FACT_SCAN_LIMIT = 1000
_EPISODE_SCAN_LIMIT = 200

# How much of a forget the reply note enumerates before summarising.
_NOTE_MAX_ITEMS = 8
_NOTE_ITEM_CHARS = 120

_TOMBSTONE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Intent detection (pure — table-testable, no store, no clock)
# ---------------------------------------------------------------------------

@dataclass
class ForgetIntent:
    """kind: "topic" (delete what matches topic), "referent"
    ("forget that" — the most recently learned fact), "global"
    ("forget everything" — never executed from chat), "undo"."""

    kind: str
    topic: str = ""


_LEADING_POLITENESS = re.compile(
    r"^\s*(?:please\s+|ok(?:ay)?[,\s]+|hey\s+|aether[,\s]+|so\s+)+",
    re.IGNORECASE,
)
# "Don't forget ..." is a REMEMBER request (the calendar layer's
# home turf), and "never forget X" is an exhortation — neither is a
# forget command, however the rest of the sentence reads.
_NOT_A_FORGET = re.compile(
    r"\b(?:don'?t|do not|never)\s+forget\b", re.IGNORECASE)
_QUESTION_START = re.compile(
    r"^\s*(what|when|where|who|why|how|is|are|do|does|did|can|could|"
    r"would|should|will)\b",
    re.IGNORECASE,
)

_UNDO_RE = re.compile(
    r"^undo\s+(?:that\s+|the\s+|my\s+)?(?:last\s+)?forget\b", re.IGNORECASE)
_FORGET_ALL_ABOUT_RE = re.compile(
    r"^forget\s+(?:everything|all)\s+about\s+(.+)$", re.IGNORECASE)
_GLOBAL_RE = re.compile(
    r"^forget\s+(?:everything|it all|all of it|all)\b", re.IGNORECASE)
_REMOVE_FROM_MEMORY_RE = re.compile(
    r"^(?:remove|delete|erase)\s+(.+?)\s+from\s+(?:your\s+|the\s+)?memory\b",
    re.IGNORECASE)
_DONT_REMEMBER_RE = re.compile(
    r"^(?:don'?t|do not)\s+remember\s+(.+)$", re.IGNORECASE)
_FORGET_ABOUT_RE = re.compile(r"^forget\s+about\s+(.+)$", re.IGNORECASE)
_FORGET_RE = re.compile(r"^forget\s+(.+)$", re.IGNORECASE)

# Topics that mean "everything, no referent" even after the verb
# phrase is stripped ("forget all of it" reaches _clean_topic as
# "all of it" via the generic pattern on some phrasings).
_GLOBAL_TOPICS = {
    "everything", "all", "it all", "all of it", "all my memories",
    "everything you know", "everything you remember",
    "everything about me",
}

# Topics that point at the conversation, not at a stored subject:
# "forget that" refers to whatever was most recently learned.
_REFERENT_TOPICS = {
    "that", "it", "this", "that thing", "that fact", "that stuff",
    "the last thing", "the last fact", "what you just learned",
    "what you just said", "what i just said",
}

# Words that carry no referent signal when resolving a topic —
# including the weak nouns of the referent phrases, so a topic made
# only of them ("that thing we discussed" leaves "discussed", a
# real token; "that thing" leaves nothing) classifies by what
# remains.
_TOPIC_STOPWORDS = {
    "my", "the", "a", "an", "about", "that", "this", "it", "please",
    "from", "your", "you", "know", "of", "to", "in", "on", "me",
    "thing", "things", "stuff", "what", "just", "said", "told",
    "not", "no",
}

# The referent path ("forget that" = the most recent fact) only
# fires for short, command-shaped messages: in a longer sentence
# "forget that ..." is far more likely an idiom or a topic phrase,
# and a wrong deletion — even an announced, undoable one — is the
# failure this path must not have.
_REFERENT_MAX_WORDS = 10


def _topic_tokens(topic: str) -> set[str]:
    return {
        token
        for token in re.findall(r"\w+", (topic or "").casefold())
        if len(token) >= 2 and token not in _TOPIC_STOPWORDS
    }


def _clean_topic(raw: str) -> str:
    topic = " ".join((raw or "").strip().split()).strip(" .!?,")
    # Possessives/articles lead ("forget MY berlin trip"); strip
    # them repeatedly ("forget about the ...").
    while True:
        stripped = re.sub(
            r"^(?:my|the|a|an|about)\s+", "", topic, flags=re.IGNORECASE)
        if stripped == topic:
            break
        topic = stripped
    topic = re.sub(
        r"\s+from\s+(?:your\s+|the\s+)?memory$", "", topic,
        flags=re.IGNORECASE)
    return topic.strip(" .!?,").casefold()


def _classify_topic(topic: str, word_count: int) -> ForgetIntent | None:
    if topic in _GLOBAL_TOPICS:
        return ForgetIntent(kind="global")
    if topic in _REFERENT_TOPICS or not _topic_tokens(topic):
        if word_count <= _REFERENT_MAX_WORDS:
            return ForgetIntent(kind="referent")
        return None
    return ForgetIntent(kind="topic", topic=topic)


def detect_forget_intent(text: str) -> ForgetIntent | None:
    """Detect a forget/undo command in a user message, or None.

    Pure function. Conservative by design, mirroring the calendar
    extractor: the command must lead the message (after politeness
    prefixes), questions never trigger, and "don't forget" — a
    remember request — never triggers.
    """
    if not text or len(text) > 1000:
        return None
    stripped = " ".join(text.strip().split())
    body = _LEADING_POLITENESS.sub("", stripped)
    if stripped.endswith("?"):
        return None
    # Question-shaped messages never trigger — with one exemption:
    # the imperative "do not remember ..." OPENS with the question
    # word "do" but is a command, not a question (the trailing-"?"
    # check above already excluded its interrogative twin).
    if _QUESTION_START.match(stripped) and not _DONT_REMEMBER_RE.match(body):
        return None
    if _NOT_A_FORGET.search(stripped):
        return None
    word_count = len(stripped.split())

    if _UNDO_RE.match(body):
        return ForgetIntent(kind="undo")

    match = _FORGET_ALL_ABOUT_RE.match(body)
    if match:
        return _classify_topic(_clean_topic(match.group(1)), word_count)
    if _GLOBAL_RE.match(body):
        return ForgetIntent(kind="global")
    match = _REMOVE_FROM_MEMORY_RE.match(body)
    if match:
        return _classify_topic(_clean_topic(match.group(1)), word_count)
    match = _DONT_REMEMBER_RE.match(body)
    if match:
        return _classify_topic(_clean_topic(match.group(1)), word_count)
    match = _FORGET_ABOUT_RE.match(body) or _FORGET_RE.match(body)
    if match:
        return _classify_topic(_clean_topic(match.group(1)), word_count)
    return None


# ---------------------------------------------------------------------------
# Candidate finding — literal matching over the facade's records
# ---------------------------------------------------------------------------

def _row_id(row: dict[str, Any]) -> str | None:
    return row.get("fact_id") or row.get("id")


def _is_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "active") == "active"


def _text_matches(
    topic_norm: str, tokens: set[str], *texts: Any
) -> bool:
    """Literal referent match: the whole topic phrase appears in
    the text, or every topic token appears as a word in it."""
    blob = " ".join(str(t) for t in texts if t)
    blob_norm = " ".join(blob.casefold().split())
    if topic_norm and topic_norm in blob_norm:
        return True
    if tokens:
        words = set(re.findall(r"\w+", blob_norm))
        return tokens <= words
    return False


def _scan_facts(backend, topic: str) -> list[dict[str, Any]]:
    """The fact pool a forget may resolve against: the backend's
    fact list (bounded), unioned with its topic search, deduped,
    active facts only."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pools: list[list[dict[str, Any]]] = []
    try:
        pools.append(backend.list_facts(limit=_FACT_SCAN_LIMIT) or [])
    except Exception:
        logger.debug("Forget: fact list failed", exc_info=True)
    if topic:
        try:
            pools.append(backend.search_facts(topic, limit=50) or [])
        except Exception:
            logger.debug("Forget: fact search failed", exc_info=True)
    for pool in pools:
        for row in pool:
            fid = _row_id(row)
            if fid and fid not in seen and _is_active(row):
                seen.add(fid)
                rows.append(row)
    return rows


def find_forget_candidates(
    backend,
    topic: str,
    *,
    fact_ids: list[str] | None = None,
    episode_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve a topic (or explicit ids) to the concrete records a
    forget would delete — WITHOUT deleting anything.

    Facts match on entity name + fact text, episodes on prompt /
    summary / outcome, lessons on lesson text, all by the literal
    rule in :func:`_text_matches`. Returns
    ``{"facts": [...], "episodes": [...], "lessons": [...]}`` of
    facade-shaped rows / lesson strings.
    """
    topic_norm = " ".join((topic or "").casefold().split())
    tokens = _topic_tokens(topic or "")

    facts: list[dict[str, Any]] = []
    pool = _scan_facts(backend, topic or "")
    if fact_ids:
        wanted = set(fact_ids)
        facts = [row for row in pool if _row_id(row) in wanted]
    elif topic_norm:
        facts = [
            row for row in pool
            if _text_matches(
                topic_norm, tokens,
                row.get("entity_name"), row.get("fact_text"))
        ]

    episodes: list[dict[str, Any]] = []
    try:
        episode_pool = backend.list_episodes(limit=_EPISODE_SCAN_LIMIT) or []
    except Exception:
        logger.debug("Forget: episode list failed", exc_info=True)
        episode_pool = []
    if episode_ids:
        wanted_eps = set(episode_ids)
        episodes = [
            row for row in episode_pool
            if row.get("task_id") in wanted_eps
        ]
    elif topic_norm:
        episodes = [
            row for row in episode_pool
            if _text_matches(
                topic_norm, tokens,
                row.get("user_prompt"), row.get("task_summary"),
                row.get("outcome"))
        ]

    lessons: list[str] = []
    if topic_norm:
        lessons = [
            lesson for lesson in learned.load_lessons()
            if _text_matches(topic_norm, tokens, lesson)
        ]

    return {
        "facts": facts[:MAX_FORGET_ITEMS],
        "episodes": episodes[:MAX_FORGET_ITEMS],
        "lessons": lessons,
    }


# ---------------------------------------------------------------------------
# Tombstone store — bounded, expiring, atomic (learned.py discipline)
# ---------------------------------------------------------------------------

def _load_tombstones() -> list[dict[str, Any]]:
    try:
        data = json.loads(TOMBSTONES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    items = data.get("tombstones") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    cutoff = time.time() - TOMBSTONE_TTL_SECONDS
    return [
        item for item in items
        if isinstance(item, dict)
        and float(item.get("created_ts") or 0) >= cutoff
    ]


def _write_tombstones(items: list[dict[str, Any]]) -> None:
    TOMBSTONES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": 1, "tombstones": items[-MAX_TOMBSTONES:]},
        ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        dir=TOMBSTONES_PATH.parent, prefix=TOMBSTONES_PATH.name,
        suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, TOMBSTONES_PATH)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_tombstone(tombstone: dict[str, Any]) -> None:
    with _TOMBSTONE_LOCK:
        items = _load_tombstones()
        items.append(tombstone)
        _write_tombstones(items)


def _consume_tombstone(tombstone_id: str) -> None:
    with _TOMBSTONE_LOCK:
        items = _load_tombstones()
        for item in items:
            if item.get("id") == tombstone_id:
                item["consumed_ts"] = int(time.time())
        _write_tombstones(items)


def latest_tombstone() -> dict[str, Any] | None:
    """The most recent unconsumed tombstone in its undo window."""
    for item in reversed(_load_tombstones()):
        if not item.get("consumed_ts"):
            return item
    return None


# ---------------------------------------------------------------------------
# Panel discovery + history (SPEC-6B C2 — the Forget centre)
# ---------------------------------------------------------------------------

# How many distinct topics one discovery pass will scan with the
# per-topic finder. Candidate topics are ranked by their stored
# counts before scanning, so the cap keeps a panel call bounded on
# a large graph while covering every topic a user plausibly means.
MAX_DISCOVERY_TOPICS = 25


def _episode_tags(row: dict[str, Any]) -> list[str]:
    """An episode row's tags as a list — the store persists them
    as one comma-joined string; a backend row may carry a list."""
    raw = row.get("tags")
    if isinstance(raw, list):
        return [str(tag).strip() for tag in raw if str(tag).strip()]
    if isinstance(raw, str):
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
    return []


def discover_forget_topics(
    backend, *, max_topics: int = MAX_DISCOVERY_TOPICS
) -> list[dict[str, Any]]:
    """SPEC-6B C2 topic discovery: the distinct topics a panel
    forget could target, ranked by item count —
    ``[{topic, fact_count, episode_count, last_touched}…]``.

    A "topic" here is exactly what :func:`find_forget_candidates`
    can match again: entity names from the stored facts (the
    matcher's fact leg keys on entity name + fact text) and tags
    from the stored episodes (whose text the matcher's episode
    leg scans). Each candidate is counted by running the finder
    itself, so the counts are what a forget of that topic would
    actually touch — never a parallel matching rule that could
    drift from the deleter's. Topics matching nothing (a tag the
    episode text never repeats, say) are dropped.

    Bounded: the pools are the finder's own scan limits
    (``_FACT_SCAN_LIMIT`` facts, ``_EPISODE_SCAN_LIMIT``
    episodes), and at most ``max_topics`` topics are scanned —
    entities first, ranked by fact count, then tags the entities
    did not already name. One finder run per topic is the cost;
    the panel calls this on open, not per keystroke.
    """
    facts = _scan_facts(backend, "")
    entity_counts: dict[str, int] = {}
    for row in facts:
        name = str(row.get("entity_name") or "").strip()
        if name:
            entity_counts[name] = entity_counts.get(name, 0) + 1

    try:
        episodes = backend.list_episodes(limit=_EPISODE_SCAN_LIMIT) or []
    except Exception:
        logger.debug("Forget discovery: episode list failed", exc_info=True)
        episodes = []
    tag_counts: dict[str, int] = {}
    for row in episodes:
        for tag in _episode_tags(row):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    ranked_entities = sorted(
        entity_counts.items(),
        key=lambda kv: (-kv[1], kv[0].casefold()))
    topics = [name for name, _count in ranked_entities]
    seen = {topic.casefold() for topic in topics}
    ranked_tags = sorted(
        tag_counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
    for tag, _count in ranked_tags:
        if tag.casefold() not in seen:
            topics.append(tag)
            seen.add(tag.casefold())
    topics = topics[:max_topics]

    discovered: list[dict[str, Any]] = []
    for topic in topics:
        candidates = find_forget_candidates(backend, topic)
        fact_rows = candidates["facts"]
        episode_rows = candidates["episodes"]
        if not fact_rows and not episode_rows:
            continue
        touched = [
            row.get("timestamp")
            for row in fact_rows + episode_rows
        ]
        touched = [
            ts for ts in touched
            if isinstance(ts, (int, float)) and not isinstance(ts, bool)
        ]
        discovered.append({
            "topic": topic,
            "fact_count": len(fact_rows),
            "episode_count": len(episode_rows),
            "last_touched": max(touched) if touched else None,
        })
    discovered.sort(key=lambda entry: (
        -(entry["fact_count"] + entry["episode_count"]),
        -(entry["last_touched"] or 0),
        entry["topic"].casefold(),
    ))
    return discovered


def forget_history(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """SPEC-6B C2: the tombstone store as Forgotten history —
    ``{"entries": […], "total": n}``, newest first, paginated
    (Rule 2: limit + offset in, total out). ``undoable`` is true
    only for the latest unconsumed tombstone — the one
    :func:`restore_last_forget` would consume; consumed
    tombstones stay listed (they ARE the history) with
    ``undoable`` False."""
    latest = latest_tombstone()
    latest_id = latest.get("id") if latest else None
    entries = [
        {
            "tombstone_id": item.get("id"),
            "topic": item.get("topic", ""),
            "fact_count": len(item.get("facts") or []),
            "episode_count": len(item.get("episodes") or []),
            "forgotten_at": item.get("created_ts"),
            "undoable": bool(latest_id) and item.get("id") == latest_id,
        }
        for item in reversed(_load_tombstones())
    ]
    offset = max(0, int(offset))
    limit = max(0, int(limit))
    return {"entries": entries[offset:offset + limit], "total": len(entries)}


# ---------------------------------------------------------------------------
# Execute + restore
# ---------------------------------------------------------------------------

def _display_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "fact",
        "id": _row_id(row),
        "text": str(row.get("fact_text") or ""),
    }


def _display_episode(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "episode",
        "id": row.get("task_id"),
        "text": str(row.get("task_summary") or row.get("user_prompt") or ""),
    }


def _snapshot(
    backend, kind: str, record_id: str, facade_row: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """(snapshot, full_fidelity): the backend's own snapshot when
    it has one (native stores — original id and every stored
    field); otherwise the facade row, which is enough to REPORT
    the deletion but not to restore it by id."""
    try:
        if kind == "fact":
            snap = backend.snapshot_fact(record_id)
        else:
            snap = backend.snapshot_episode(record_id)
    except NotImplementedError:
        return dict(facade_row), False
    except Exception:
        # A snapshot failure must not veto the user's explicit
        # delete — but the deletion then proceeds WITHOUT the undo
        # guarantee, and the result says so (fidelity False).
        logger.warning(
            "Forget: %s snapshot failed for %s; deleting without "
            "undo fidelity", kind, record_id, exc_info=True)
        return dict(facade_row), False
    if snap is not None:
        return dict(snap), True
    return dict(facade_row), False


def _regen_curated(config) -> bool:
    """Regenerate the curated document from the live stores so a
    forgotten record cannot resurface through it (and a restored
    one reappears). Best-effort: False when the pass did not
    write, with the reason on the log — never an exception."""
    try:
        from aether_engine.memory import consolidation

        result = consolidation.consolidate_memory_document(config)
        return bool(result.get("written"))
    except Exception:
        logger.debug("Forget: curated regeneration failed", exc_info=True)
        return False


def _execute_candidates(
    backend,
    candidates: dict[str, Any],
    *,
    topic: str,
    phrase: str,
    config=None,
    truncated_candidates: int = 0,
) -> dict[str, Any]:
    """Delete the resolved candidates across every surface, with
    tombstone snapshots taken BEFORE each delete. Shared by the
    topic path and the referent path so both honour the identical
    contract."""
    deleted_facts: list[dict[str, Any]] = []
    deleted_episodes: list[dict[str, Any]] = []
    fact_snaps: list[dict[str, Any]] = []
    episode_snaps: list[dict[str, Any]] = []
    full_fidelity = True

    for row in candidates.get("facts", []):
        fid = _row_id(row)
        if not fid:
            continue
        snap, fidelity = _snapshot(backend, "fact", fid, row)
        if backend.delete_fact(fid):
            deleted_facts.append(_display_fact(row))
            fact_snaps.append(snap)
            full_fidelity = full_fidelity and fidelity

    for row in candidates.get("episodes", []):
        task_id = row.get("task_id")
        if not task_id:
            continue
        snap, fidelity = _snapshot(backend, "episode", task_id, row)
        if backend.delete_episode(task_id):
            deleted_episodes.append(_display_episode(row))
            episode_snaps.append(snap)
            full_fidelity = full_fidelity and fidelity

    removed_lessons = (
        learned.remove_lessons(candidates.get("lessons", []))
        if candidates.get("lessons")
        else []
    )
    deleted_lessons = [
        {"kind": "lesson", "id": None, "text": text}
        for text in removed_lessons
    ]

    anything = bool(deleted_facts or deleted_episodes or deleted_lessons)
    tombstone_id = None
    if anything:
        tombstone_id = uuid.uuid4().hex
        _append_tombstone({
            "id": tombstone_id,
            "created_ts": int(time.time()),
            "consumed_ts": None,
            "phrase": (phrase or "")[:200],
            "topic": topic,
            "backend": getattr(backend, "name", "unknown"),
            "undo_supported": full_fidelity,
            "facts": fact_snaps,
            "episodes": episode_snaps,
            "lessons": removed_lessons,
        })

    curated_regenerated = False
    if deleted_facts or deleted_episodes:
        curated_regenerated = _regen_curated(config)

    result = {
        "topic": topic,
        "deleted": {
            "facts": deleted_facts,
            "episodes": deleted_episodes,
            "lessons": deleted_lessons,
        },
        "counts": {
            "facts": len(deleted_facts),
            "episodes": len(deleted_episodes),
            "lessons": len(deleted_lessons),
        },
        "tombstone_id": tombstone_id,
        "undo_supported": bool(anything and full_fidelity),
        "unsupported": (
            []
            if full_fidelity
            else [(
                "undo is unsupported on the "
                f"{getattr(backend, 'name', 'active')} backend: it "
                "has no snapshot/restore by original id, so this "
                "deletion cannot be restored from chat"
            )]
        ),
        "curated_regenerated": curated_regenerated,
        "truncated_candidates": truncated_candidates,
    }
    _audit_logger.log_event("MEMORY_FORGET_EXECUTED", {
        "topic": topic,
        "backend": getattr(backend, "name", "unknown"),
        "facts": [d["id"] for d in deleted_facts],
        "episodes": [d["id"] for d in deleted_episodes],
        "lessons": len(deleted_lessons),
        "tombstone_id": tombstone_id,
        "undo_supported": result["undo_supported"],
        "truncated_candidates": truncated_candidates,
    })
    return result


def execute_forget(
    backend,
    topic: str,
    *,
    phrase: str = "",
    fact_ids: list[str] | None = None,
    episode_ids: list[str] | None = None,
    config=None,
) -> dict[str, Any]:
    """Find + delete everything matching ``topic`` (or the explicit
    ids), across facts, episodes, lessons and the curated document.
    The result lists exactly what was deleted — empty lists mean
    nothing matched and nothing was deleted."""
    candidates = find_forget_candidates(
        backend, topic, fact_ids=fact_ids, episode_ids=episode_ids)
    total = (
        len(candidates["facts"]) + len(candidates["episodes"])
        + len(candidates["lessons"])
    )
    truncated = 0
    if total > MAX_FORGET_ITEMS:
        # The per-operation cap is a hard bound, not a target:
        # trim in a fixed order (facts, episodes, lessons) and
        # REPORT the overflow — the caller must never believe the
        # whole topic went away when the cap left some behind.
        truncated = total - MAX_FORGET_ITEMS
        remaining = MAX_FORGET_ITEMS
        for key in ("facts", "episodes", "lessons"):
            candidates[key] = candidates[key][:remaining]
            remaining -= len(candidates[key])
    return _execute_candidates(
        backend, candidates, topic=topic or "", phrase=phrase,
        config=config, truncated_candidates=truncated)


def forget_most_recent_fact(
    backend, *, phrase: str = "", config=None
) -> dict[str, Any]:
    """The referent path: "forget that" deletes the single most
    recently stored ACTIVE fact — the thing the assistant most
    plausibly just learned. Nothing else is touched."""
    rows = [
        row for row in _scan_facts(backend, "")
    ]
    rows.sort(
        key=lambda r: (-(r.get("timestamp") or 0), str(_row_id(r) or "")))
    candidates = {
        "facts": rows[:1],
        "episodes": [],
        "lessons": [],
    }
    return _execute_candidates(
        backend, candidates, topic="", phrase=phrase, config=config)


def restore_last_forget(backend, *, config=None) -> dict[str, Any] | None:
    """Undo the most recent forget: restore its tombstone's records
    with their original ids/fields and mark the tombstone consumed.

    Returns None when there is nothing to undo. A tombstone left by
    a DIFFERENT backend is not consumed and not restored — undo
    never writes one store's records into another."""
    tombstone = latest_tombstone()
    if tombstone is None:
        return None
    backend_name = getattr(backend, "name", "unknown")
    if tombstone.get("backend") not in (None, backend_name):
        return {
            "tombstone_id": tombstone.get("id"),
            "topic": tombstone.get("topic", ""),
            "error": (
                f"the last forget was recorded on the "
                f"{tombstone.get('backend')} backend, not the active "
                f"{backend_name} backend — nothing was restored"
            ),
            "restored": {"facts": [], "episodes": [], "lessons": []},
            "unrestored": [],
        }

    restored_facts: list[dict[str, Any]] = []
    restored_episodes: list[dict[str, Any]] = []
    unrestored: list[dict[str, Any]] = []
    for snap in tombstone.get("facts", []):
        display = _display_fact(snap)
        try:
            ok = backend.restore_fact_snapshot(snap)
        except NotImplementedError:
            ok = False
        (restored_facts if ok else unrestored).append(display)
    for snap in tombstone.get("episodes", []):
        display = _display_episode(snap)
        try:
            ok = backend.restore_episode_snapshot(snap)
        except NotImplementedError:
            ok = False
        (restored_episodes if ok else unrestored).append(display)

    restored_lessons: list[dict[str, Any]] = []
    lesson_texts = tombstone.get("lessons", [])
    if lesson_texts:
        learned.record_lessons(lesson_texts, task_id="forget-undo")
        present = {
            learned._normalise_lesson(text)
            for text in learned.load_lessons()
        }
        for text in lesson_texts:
            display = {"kind": "lesson", "id": None, "text": text}
            if learned._normalise_lesson(text) in present:
                restored_lessons.append(display)
            else:
                unrestored.append(display)

    _consume_tombstone(tombstone["id"])
    curated_regenerated = False
    if restored_facts or restored_episodes:
        curated_regenerated = _regen_curated(config)

    result = {
        "tombstone_id": tombstone.get("id"),
        "topic": tombstone.get("topic", ""),
        "phrase": tombstone.get("phrase", ""),
        "restored": {
            "facts": restored_facts,
            "episodes": restored_episodes,
            "lessons": restored_lessons,
        },
        "unrestored": unrestored,
        "curated_regenerated": curated_regenerated,
    }
    _audit_logger.log_event("MEMORY_FORGET_RESTORED", {
        "tombstone_id": tombstone.get("id"),
        "backend": backend_name,
        "facts": [d["id"] for d in restored_facts],
        "episodes": [d["id"] for d in restored_episodes],
        "lessons": len(restored_lessons),
        "unrestored": len(unrestored),
    })
    return result


# ---------------------------------------------------------------------------
# Chat note hook — the calendar C2 pattern (extraction.py):
# engine-side action, then a system note so the reply announces
# exactly what happened. Never raises; a failure means no note.
# ---------------------------------------------------------------------------

def _describe_items(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items[:_NOTE_MAX_ITEMS]:
        text = " ".join(str(item.get("text") or "").split())
        if len(text) > _NOTE_ITEM_CHARS:
            text = text[:_NOTE_ITEM_CHARS].rstrip() + "…"
        if item.get("kind") == "fact":
            parts.append(f'a fact: "{text}"')
        elif item.get("kind") == "episode":
            parts.append(f'a past task record: "{text}"')
        else:
            parts.append(f'a lesson: "{text}"')
    extra = len(items) - len(parts)
    if extra > 0:
        parts.append(f"and {extra} more item(s)")
    return "; ".join(parts)


def _deleted_note(result: dict[str, Any], description: str) -> str:
    deleted = result["deleted"]
    items = deleted["facts"] + deleted["episodes"] + deleted["lessons"]
    note = (
        f"[Memory] The user asked to forget {description}. The "
        f"engine has already deleted the following from long-term "
        f"memory: {_describe_items(items)}. In your reply, state "
        "exactly what was forgotten — name the items — and do not "
        "claim anything else was deleted."
    )
    if result["undo_supported"]:
        note += ' Tell the user they can say "undo forget" to restore it.'
    else:
        note += (
            " This deletion cannot be undone from chat with the "
            "active memory backend; do not promise an undo.")
    if result.get("truncated_candidates"):
        note += (
            f" {result['truncated_candidates']} further matching "
            "item(s) were NOT deleted (the per-request cap) — say "
            "that too, and that repeating the request forgets more.")
    return note


def forget_note_for_prompt(
    text: str, backend, *, config=None
) -> str | None:
    """Detect + act + note for one chat message, or None when the
    message is not a forget/undo command."""
    intent = detect_forget_intent(text)
    if intent is None:
        return None

    if intent.kind == "undo":
        result = restore_last_forget(backend, config=config)
        if result is None:
            return (
                "[Memory] The user asked to undo a forget, but "
                "there is nothing to undo — no recent forget is "
                "waiting to be reversed. Say so plainly, and do not "
                "claim anything was restored."
            )
        if result.get("error"):
            return (
                f"[Memory] The user asked to undo the last forget, "
                f"but it could not be restored: {result['error']}. "
                "Nothing was restored. Say so plainly."
            )
        restored = result["restored"]
        items = (
            restored["facts"] + restored["episodes"]
            + restored["lessons"]
        )
        note = (
            "[Memory] The user asked to undo the last forget. The "
            f"engine restored: {_describe_items(items)}. Confirm "
            "in your reply exactly what is back in memory, and do "
            "not claim anything else was restored."
        )
        if result["unrestored"]:
            note += (
                f" {_describe_items(result['unrestored'])} could "
                "not be restored — say that plainly too."
            )
        return note

    if intent.kind == "global":
        return (
            "[Memory] The user asked to forget everything. That "
            "was NOT done: conversational forget removes specific "
            "remembered items only, and nothing was deleted. Tell "
            "the user nothing was deleted, that they can name a "
            "specific topic to forget here, and that a full memory "
            "clear is available in Settings → Memory Management."
        )

    if intent.kind == "referent":
        result = forget_most_recent_fact(
            backend, phrase=text, config=config)
        if not result["deleted"]["facts"]:
            return (
                "[Memory] The user asked to forget the most "
                "recently learned item, but long-term memory holds "
                "no facts — nothing was deleted. Say exactly that; "
                "do not claim a deletion happened."
            )
        return _deleted_note(result, "the most recently learned item")

    result = execute_forget(
        backend, intent.topic, phrase=text, config=config)
    counts = result["counts"]
    if not (counts["facts"] or counts["episodes"] or counts["lessons"]):
        return (
            f'[Memory] The user asked to forget "{intent.topic}", '
            "but nothing in long-term memory matched, so NOTHING "
            "was deleted. Say exactly that — nothing matched, "
            "nothing was forgotten. Do not claim any deletion "
            "happened."
        )
    return _deleted_note(result, f'"{intent.topic}"')


def note_for_message_if_enabled(
    text: str, *, user_config=None
) -> str | None:
    """Full entry point for the chat paths: resolve the configured
    backend and run detect + act + note. Mirrors the calendar
    hook's contract — any failure means no note, never a broken
    turn; an unavailable backend means no note either (memory's
    absence is already logged loudly by backend_or_none)."""
    try:
        config = user_config
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        from aether_engine.memory.facade import backend_or_none

        backend = backend_or_none(config, context="conversational forget")
        if backend is None:
            return None
        return forget_note_for_prompt(text, backend, config=config)
    except Exception:
        logger.debug("Forget note hook failed", exc_info=True)
        return None
