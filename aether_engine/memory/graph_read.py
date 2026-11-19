"""Full Furnish Wave 6 (SPEC-6B C5, per D-4): graph neighbourhood.

The local graph view's read: one entity at the centre, the
entities it co-occurs with around it, and the supersession
chains that cross it. There is NO edges table and this module
adds none (FF5-01's gap, closed the D-4 way): every edge is
DERIVED AT READ TIME from the stored records —

* **co-occurrence** — two entities appearing in the same fact
  (a fact's owning entity plus every entity its text names) or
  the same episode (every entity its prompt/summary/outcome
  names). The count is the number of shared records.
* **replaced chains** — the KG's existing superseded-by links:
  a superseded fact whose ``superseded_by`` points at a fact of
  another entity is one replacement link between the two.

Reads go through the facade surface only (``list_facts`` with
history, ``list_episodes``), like every other memory consumer.
Fact counts and co-occurrence describe ACTIVE memory (what
Aether currently holds as true); superseded facts contribute
only the replaced chains, which are history by nature.

Depth: 1 lists the centre's direct neighbours. 2 also lists
neighbours of neighbours — every entry's ``cooccurrence_count``
is still its DIRECT count against the centre (recomputed
against the centre, per the contract), and each entry carries
its ``depth`` so the surface can tell a direct neighbour from
one reached through another entity.

An unknown entity is an honest empty result naming the entity
as asked — never an error. A backend that cannot list episodes
loses only the episode leg (episode counts read null); a
backend that cannot list facts cannot serve the read at all
and its error propagates.
"""
from __future__ import annotations

import logging
import re
from itertools import combinations
from typing import Any

logger = logging.getLogger("aether_engine.memory.graph_read")

# Same scan discipline as memory/stats.py: retention caps keep
# real stores far below these bounds.
_FACT_SCAN_LIMIT = 100_000
_EPISODE_SCAN_LIMIT = 100_000


def _mention_re(name: str) -> re.Pattern[str]:
    """Whole-name mention matcher (case-insensitive)."""
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)


def _episode_text(episode: dict) -> str:
    return " ".join(
        str(episode.get(key) or "")
        for key in ("user_prompt", "task_summary", "outcome"))


def graph_neighbourhood(
    backend,
    entity_name: str,
    depth: int = 1,
) -> dict[str, Any]:
    """The C5 neighbourhood shape for ``entity_name``.

    ``{centre: {entity_name, entity_type, fact_count,
    episode_count}, neighbours: [{entity_name, entity_type,
    cooccurrence_count, fact_count, depth}], replaced_chains:
    [{old_entity, new_entity, fact_id}]}``.
    """
    asked = str(entity_name or "")
    depth = 2 if depth == 2 else 1

    facts = backend.list_facts(
        limit=_FACT_SCAN_LIMIT, include_history=True) or []

    # The entity registry, derived from the fact listing: an
    # entity exists here because facts name it as their owner.
    entities: dict[str, dict[str, Any]] = {}
    facts_by_id: dict[str, dict] = {}
    for fact in facts:
        fact_id = str(fact.get("fact_id") or fact.get("id") or "")
        if fact_id:
            facts_by_id[fact_id] = fact
        name = str(fact.get("entity_name") or "")
        if not name:
            continue
        entry = entities.setdefault(name, {
            "entity_type": fact.get("entity_type"),
            "fact_count": 0,
        })
        if entry["entity_type"] is None:
            entry["entity_type"] = fact.get("entity_type")
        if str(fact.get("status") or "active") != "superseded":
            entry["fact_count"] += 1

    centre_name = asked if asked in entities else next(
        (name for name in entities if name.casefold() == asked.casefold()),
        None)
    if centre_name is None:
        return {
            "centre": {
                "entity_name": asked,
                "entity_type": None,
                "fact_count": 0,
                "episode_count": 0,
            },
            "neighbours": [],
            "replaced_chains": [],
        }

    matchers = {name: _mention_re(name) for name in entities}

    def participants(text: str, owner: str | None) -> set[str]:
        found = {owner} if owner else set()
        for name, pattern in matchers.items():
            if name != owner and pattern.search(text):
                found.add(name)
        return found

    # Pairwise co-occurrence over active facts and episodes.
    pair_counts: dict[tuple[str, str], int] = {}

    def _add_pairs(members: set[str]) -> None:
        for a, b in combinations(sorted(members), 2):
            pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1

    for fact in facts:
        if str(fact.get("status") or "active") == "superseded":
            continue
        owner = str(fact.get("entity_name") or "")
        if not owner:
            continue
        _add_pairs(participants(str(fact.get("fact_text") or ""), owner))

    episode_count: int | None = 0
    try:
        episodes = backend.list_episodes(limit=_EPISODE_SCAN_LIMIT) or []
    except Exception:  # the episode leg degrades alone
        logger.warning(
            "graph_neighbourhood: episode listing failed", exc_info=True)
        episodes = None
    if episodes is None:
        episode_count = None
    else:
        for episode in episodes:
            members = participants(_episode_text(episode), None)
            if centre_name in members:
                episode_count += 1
            _add_pairs(members)

    def direct_count(name: str) -> int:
        key = (centre_name, name) if centre_name < name else (name, centre_name)
        return pair_counts.get(key, 0)

    def neighbours_of(name: str) -> set[str]:
        found = set()
        for (a, b) in pair_counts:
            if a == name:
                found.add(b)
            elif b == name:
                found.add(a)
        return found

    depth_of: dict[str, int] = {}
    for name in neighbours_of(centre_name):
        depth_of[name] = 1
    if depth == 2:
        for name in list(depth_of):
            for other in neighbours_of(name):
                if other != centre_name and other not in depth_of:
                    depth_of[other] = 2

    neighbours = [{
        "entity_name": name,
        "entity_type": entities[name]["entity_type"],
        "cooccurrence_count": direct_count(name),
        "fact_count": entities[name]["fact_count"],
        "depth": level,
    } for name, level in depth_of.items()]
    neighbours.sort(key=lambda n: (
        n["depth"], -n["cooccurrence_count"], n["entity_name"].casefold()))

    displayed = set(depth_of) | {centre_name}
    replaced_chains: list[dict[str, Any]] = []
    for fact in facts:
        if str(fact.get("status") or "active") != "superseded":
            continue
        superseding = facts_by_id.get(str(fact.get("superseded_by") or ""))
        if superseding is None:
            continue
        old_entity = str(fact.get("entity_name") or "")
        new_entity = str(superseding.get("entity_name") or "")
        if not old_entity or not new_entity or old_entity == new_entity:
            continue
        if old_entity not in displayed and new_entity not in displayed:
            continue
        replaced_chains.append({
            "old_entity": old_entity,
            "new_entity": new_entity,
            "fact_id": str(fact.get("fact_id") or fact.get("id") or ""),
        })

    return {
        "centre": {
            "entity_name": centre_name,
            "entity_type": entities[centre_name]["entity_type"],
            "fact_count": entities[centre_name]["fact_count"],
            "episode_count": episode_count,
        },
        "neighbours": neighbours,
        "replaced_chains": replaced_chains,
    }
