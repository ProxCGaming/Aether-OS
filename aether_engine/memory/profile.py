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
    return {
        "fact_id": str(fact.get("fact_id") or fact.get("id") or ""),
        "entity_name": str(fact.get("entity_name") or ""),
        "entity_type": str(fact.get("entity_type") or ""),
        "text": text,
        "provenance": provenance if provenance in _PROVENANCE_KINDS else "inferred",
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


def build_user_profile(backend, *, fact_limit: int = PROFILE_FACT_LIMIT) -> dict[str, Any]:
    """Derive the profile from the backend's active facts.

    ``backend`` is a facade MemoryBackend — this module never
    talks to a store directly. Returns
    ``{"user": [items], "people": {entity_name: [items]},
    "preferences": [items]}``; every section is empty (never an
    error) when nothing matching is stored. People entities keep
    first-appearance order under the global trust sort, so the
    person Aether is most confident about leads.
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
    return {"user": user, "people": people, "preferences": preferences}


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
