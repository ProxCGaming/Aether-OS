"""S6: the user/relationship profile — a VIEW over the knowledge
graph, not a store.

The audit gap this closes (master-build plan R9, audit-memory-eq
§2): the "user / relationship model" layer did not exist — what
Aether knows about the user sat as undifferentiated KG facts, and
nothing could answer "who is this user, who are their people,
what do they prefer, and how sure are we?" This module answers it
by DERIVING a profile from the existing facts on every call:

* No new tables, no duplicated facts, no write path of its own.
  Corrections happen where they always have — the facade's
  update_fact / delete_fact / delete_entity — and the next
  profile read reflects them, because there is nothing here to
  fall out of sync.
* Facts come through the facade's ``list_facts`` (active only —
  superseded facts are history and NEVER appear in a profile),
  so the view works over whichever backend is configured, with
  the same honest limits as that backend's own reads.
* Classification is structural, by the entity each fact hangs
  off and the entity's stored type — exactly as recorded, never
  re-guessed from fact text:
    - facts about the designated user entity (an entity named
      "user" / "the user", case-insensitive — the subject the
      extraction prompt itself uses) → the user section;
    - facts about other entities typed Person → people &
      relationships, grouped by entity;
    - facts about entities typed Preference → preferences;
    - everything else (projects, concepts, ...) is knowledge,
      not profile, and is left out.
  A fact lands in exactly ONE section, in that precedence order
  — a preference fact recorded on the user entity is part of
  "about the user", not repeated under Preferences.
* Ordering inside a section follows S5's trust signals: stated
  facts before inferred ones, then importance, confidence, and
  recency. Every item carries its fact id and its provenance,
  so a profile statement is never detached from the stored
  record it came from — and the briefing renderer labels
  inferred items "(inferred)": the model reading the profile
  can see which parts Aether was told and which it derived.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("aether_engine.memory.profile")

# The briefing cap — the house briefing pattern (learned_briefing
# 2,000 / blackboard_briefing 1,200): a hard ceiling the rendered
# profile never exceeds, truncation marker included.
PROFILE_BRIEFING_MAX_CHARS = 2000

# How many of the backend's facts (its native order: confidence,
# then recency) the view reads. Deliberately far above any
# plausible user/people/preference subset — those entities are a
# minority of the graph — while keeping the derivation bounded.
PROFILE_FACT_LIMIT = 1000

# The designated user entity's names, casefolded. The extraction
# prompt's own examples use "User" as the subject of user facts.
USER_ENTITY_NAMES = frozenset({"user", "the user"})

_PROVENANCE_KINDS = ("stated", "inferred")


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _source_label(provenance: str, timestamp: Any) -> str:
    """The human provenance string the profile surface shows
    (SPEC-6B C1): what the user asserted themselves reads "Set by
    you"; what the engine derived from conversation reads
    "From chat · <day> <Mon>" — the fact's own date, local time —
    or plain "From chat" when the record carries no usable
    timestamp. Derived here, at the facade's view, so no surface
    ever formats raw provenance itself."""
    if provenance == "stated":
        return "Set by you"
    ts = 0.0
    if not isinstance(timestamp, bool):
        try:
            ts = float(timestamp)
        except (TypeError, ValueError):
            ts = 0.0
    if ts > 0:
        dt = datetime.fromtimestamp(ts)
        return f"From chat · {dt.day} {dt.strftime('%b')}"
    return "From chat"


def _profile_item(fact: dict) -> dict | None:
    """One stored fact as a profile item, or None when the row
    carries no text to profile. Provenance follows the S5 rule:
    only an explicit 'stated' earns the stated kind — anything
    missing or junk (external backends that predate the field,
    pre-S5 rows) is 'inferred', the kind that must be labelled
    and may decay."""
    text = str(fact.get("fact_text") or "").strip()
    if not text:
        return None
    provenance = str(fact.get("provenance") or "").strip().lower()
    provenance = provenance if provenance in _PROVENANCE_KINDS else "inferred"
    return {
        "fact_id": str(fact.get("fact_id") or fact.get("id") or ""),
        "entity_name": str(fact.get("entity_name") or ""),
        "entity_type": str(fact.get("entity_type") or ""),
        "text": text,
        "provenance": provenance,
        "source_label": _source_label(provenance, fact.get("timestamp")),
        "importance": _number(fact.get("importance"), 0.5),
        "confidence": _number(fact.get("confidence"), 0.0),
        "timestamp": fact.get("timestamp"),
    }


def _item_sort_key(item: dict):
    """Stated before inferred, then importance, confidence,
    recency — the S5 trust order, so the profile leads with what
    the user actually said about themselves."""
    try:
        timestamp = int(item.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return (
        0 if item["provenance"] == "stated" else 1,
        -item["importance"],
        -item["confidence"],
        -timestamp,
        item["fact_id"],
    )


def _page(items: list[dict], limit: int | None, offset: int) -> list[dict]:
    """One page of a section (SPEC-6B Rule 2): limit=None returns
    the whole section, as the view always did before pagination."""
    if limit is None:
        return items[offset:]
    return items[offset:offset + limit]


def _page_people(
    people: dict[str, list[dict]], limit: int | None, offset: int
) -> dict[str, list[dict]]:
    """Page the people section as ONE list contract: its items in
    the section's grouped order (person by person, first-appearance
    order), sliced, then regrouped — a person whose items all fall
    outside the page does not appear."""
    flat = [item for items in people.values() for item in items]
    paged: dict[str, list[dict]] = {}
    for item in _page(flat, limit, offset):
        paged.setdefault(item["entity_name"], []).append(item)
    return paged


def build_user_profile(backend, *, fact_limit: int = PROFILE_FACT_LIMIT,
                       limit: int | None = None,
                       offset: int = 0,
                       section_pages: dict | None = None) -> dict[str, Any]:
    """Derive the profile from the backend's active facts.

    ``backend`` is a facade MemoryBackend — this module never
    talks to a store directly. Returns
    ``{"user": [items], "people": {entity_name: [items]},
    "preferences": [items], "totals": {"user": n, "people": n,
    "preferences": n}}``; every section is empty (never an error)
    when nothing matching is stored. People entities keep
    first-appearance order under the global trust sort, so the
    person Aether is most confident about leads.

    Pagination (SPEC-6B C1, Rule 2): ``limit`` + ``offset`` apply
    per section — the people section pages over its items in
    grouped order — and ``totals`` always reports the unpaged
    per-section item counts. The defaults (limit=None) reproduce
    the full sections exactly as before pagination existed.
    """
    facts = backend.list_facts(limit=fact_limit) or []
    items = [item for item in (_profile_item(f) for f in facts) if item]
    items.sort(key=_item_sort_key)

    user: list[dict] = []
    people: dict[str, list[dict]] = {}
    preferences: list[dict] = []
    for item in items:
        if item["entity_name"].strip().casefold() in USER_ENTITY_NAMES:
            user.append(item)
        elif item["entity_type"].strip().casefold() == "person":
            people.setdefault(item["entity_name"], []).append(item)
        elif item["entity_type"].strip().casefold() == "preference":
            preferences.append(item)
    offset = max(0, int(offset))
    if limit is not None:
        limit = max(0, int(limit))

    def _section_page(name: str) -> tuple[int | None, int]:
        # Per-section paging (the Profile panel pages each section
        # independently); a section absent from section_pages falls
        # back to the call-wide limit/offset.
        if section_pages and name in section_pages:
            s_limit, s_offset = section_pages[name]
            s_offset = max(0, int(s_offset))
            if s_limit is not None:
                s_limit = max(0, int(s_limit))
            return s_limit, s_offset
        return limit, offset

    user_limit, user_offset = _section_page("user")
    people_limit, people_offset = _section_page("people")
    pref_limit, pref_offset = _section_page("preferences")
    totals = {
        "user": len(user),
        "people": sum(len(person_items) for person_items in people.values()),
        "preferences": len(preferences),
    }
    return {
        "user": _page(user, user_limit, user_offset),
        "people": _page_people(people, people_limit, people_offset),
        "preferences": _page(preferences, pref_limit, pref_offset),
        "totals": totals,
    }


def _render_line(item: dict, *, with_entity: bool = False) -> str:
    """One briefing line: the fact text, an (inferred) label for
    derived facts (stated facts need no label — they are the
    user's own words, and the label budget is spent where the
    doubt is), and the source fact id."""
    label = " (inferred)" if item["provenance"] == "inferred" else ""
    prefix = f"{item['entity_name']}: " if with_entity else ""
    return f"- {prefix}{item['text']}{label} [fact {item['fact_id']}]"


def profile_briefing(backend, *, fact_limit: int = PROFILE_FACT_LIMIT,
                     max_chars: int = PROFILE_BRIEFING_MAX_CHARS) -> str:
    """The profile rendered as a bounded briefing — the house
    briefing contract: "" when the profile is empty (the section
    simply does not appear), and never longer than ``max_chars``,
    truncation marker included."""
    profile = build_user_profile(backend, fact_limit=fact_limit)
    if not (profile["user"] or profile["people"] or profile["preferences"]):
        return ""

    header = (
        "User profile (derived from long-term memory — what Aether "
        "has stored about the user, their people and their preferences):"
    )
    sections: list[tuple[str, list[str]]] = []
    if profile["user"]:
        sections.append((
            "About the user:",
            [_render_line(item) for item in profile["user"]],
        ))
    if profile["people"]:
        people_lines = [
            _render_line(item, with_entity=True)
            for items in profile["people"].values()
            for item in items
        ]
        sections.append(("People & relationships:", people_lines))
    if profile["preferences"]:
        sections.append((
            "Preferences:",
            [_render_line(item) for item in profile["preferences"]],
        ))

    marker = "...[profile truncated to budget]"
    lines = [header]
    used = len(header)
    truncated = False
    for title, section_lines in sections:
        for line in [title, *section_lines]:
            if used + len(line) + 1 > max_chars:
                truncated = True
                break
            lines.append(line)
            used += len(line) + 1
        if truncated:
            break
    if truncated and used + len(marker) + 1 <= max_chars:
        lines.append(marker)
    return "\n".join(lines)
