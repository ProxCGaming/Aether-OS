"""S5: the single context assembler — unified scored memory recall.

Before S5 every memory store ranked its own way and every briefing
section spent its own character cap: KG facts came back ordered by
confidence-then-timestamp from a substring search, episodes by
hybrid RRF rank (relevance only), the curated consolidation
document was written weekly and read by nobody, and no score
anywhere combined relevance with recency, confidence, or
importance (no store even had an importance field). This module is
the ONE place a memory briefing is assembled:

* Candidates come only through the memory facade (the configured
  backend) — KG facts and episodic episodes — plus the curated
  consolidation document as a third candidate source, so
  consolidation is finally consumed, not write-only (audit R18).
* Every candidate is scored with the SAME formula (below), its
  native relevance first normalised to a comparable 0..1 range,
  and the winners are rendered under ONE total token budget
  (``UserConfig.memory_context_token_budget``) instead of the old
  additive per-section caps. The supervisor memory tools keep
  their own outer result cap as a safety limit on top.
* Every rendered item carries its source id (fact id, episode
  task_id, or curated document line) — a recall used in a reply
  stays traceable to the stored record it came from.

THE SCORING FORMULA (the contract — change it here, nowhere else):

    score = relevance * recency * confidence * importance

each factor in 0..1, so the score is in 0..1:

* relevance — token overlap between the query and the candidate's
  text, computed identically for every store so the numbers are
  comparable across stores: the fraction of the query's distinct
  word tokens (casefolded, length >= 2) that appear as tokens in
  the candidate text, with an exact-phrase bonus — if the whole
  normalised query string appears in the candidate text, relevance
  is 1.0. (The stores' native rankings still decide WHICH records
  become candidates — the KG hybrid substring+semantic search,
  the episodic hybrid BM25+cosine RRF — but the facade does not
  expose those native scores, so the assembler re-derives a
  uniform lexical relevance for scoring; that is stated here, not
  hidden.) When the query
  has no usable tokens (a list-style briefing), relevance is the
  neutral 0.5 for every candidate and the other factors decide.
  With a real query, a candidate with relevance 0 is dropped: it
  shares nothing with what was asked.
  S6 amendment — a fact the KG's semantic leg attested (its
  hybrid search stamps ``semantic_similarity``, already gated at
  the store's cosine threshold) carries a second, independent
  relevance signal, and the relevance factor takes the STRONGER
  of the two: relevance = max(lexical, semantic_similarity). The
  four-factor formula is unchanged; only the relevance input
  gains the store's evidence, so a paraphrase with zero token
  overlap is not discarded when the embedding leg vouches for
  it — while a candidate no leg vouches for still drops.
* recency — exponential half-life on the record's age:
  ``0.5 ** (age_days / 90)`` (90-day half-life: memory here is
  long-term, so a month-old fact still scores ~0.79; a record with
  no timestamp scores the neutral 0.5; future timestamps clamp to
  age 0).
* confidence — the fact's stored confidence, clamped to 0..1.
  Episodes carry no confidence: an episode is a first-party record
  of a task that actually ran, written by the executor, not an
  inferred belief — its confidence factor is 1.0. A fact whose
  confidence is missing/junk scores the neutral 0.5 here (unlike
  the KG store's own ordering, which treats NULL as 0.0: that
  convention ranks STORED rows against each other, while this
  assembler also faces external backends that may not supply a
  confidence at all, and a hard 0 would make every such record
  invisible — the divergence is deliberate and documented).
* importance — the fact's stored importance (S5 schema; neutral
  0.5 when absent — pre-S5 rows migrate to exactly that default).
  Episodes and curated lines carry no importance signal: neutral
  0.5.

Curated-document candidates: the fact lines of
``curated_memory.md`` are parsed back into candidates (source id
``curated_memory.md:<line>``), scored like everything else, and
deduped against live KG facts (a line whose text matches a live
fact candidate is dropped — the live record, with its real id and
fresh factors, is the one to cite). Their confidence is the value
the document itself recorded in its ``(confidence X)`` suffix,
else neutral; their timestamp is the document's mtime (when the
consolidation was rendered). They compete inside the same budget —
no reserved share, no separate cap.

Token budget: tokens are estimated at the house 4 chars/token
(the estimate consolidation.py already documents). Assembly is
greedy in score order: a candidate whose rendered line does not
fit the remaining budget is skipped and smaller ones are still
considered, so the assembled text's estimate never exceeds the
budget.
"""
from __future__ import annotations

import inspect
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.memory.context_assembler")

# Recency half-life in days — see the module docstring.
RECENCY_HALF_LIFE_DAYS = 90.0
# House token estimate (chars per token), as in consolidation.py.
CHARS_PER_TOKEN = 4
# Candidate pool sizes per source before scoring. Deliberately
# larger than any briefing: scoring, not retrieval order, decides
# what survives, so the pool must contain the records a native
# ranking would have buried.
FACT_CANDIDATE_LIMIT = 25
EPISODE_CANDIDATE_LIMIT = 10

_HEADER = "Memory context (scored recall, best first):"
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def estimate_tokens(text: str) -> int:
    """House token estimate: ceil(chars / 4)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _query_tokens(query: str) -> list[str]:
    return [
        token for token in _TOKEN_RE.findall((query or "").casefold())
        if len(token) >= 2
    ]


def _relevance(query: str, query_tokens: list[str], text: str) -> float:
    """Uniform lexical relevance in 0..1 — see module docstring."""
    if not query_tokens:
        return 0.5
    haystack = (text or "").casefold()
    hay_tokens = set(_TOKEN_RE.findall(haystack))
    matched = sum(1 for token in set(query_tokens) if token in hay_tokens)
    relevance = matched / len(set(query_tokens))
    normalised_query = " ".join((query or "").casefold().split())
    if len(normalised_query) >= 3 and normalised_query in " ".join(haystack.split()):
        relevance = 1.0
    return relevance


def _recency(timestamp: Any, now: int) -> float:
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return 0.5
    age_days = max(0, now - stamp) / 86400.0
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def _unit(value: Any, default: float) -> float:
    """A 0..1 factor from a stored value; `default` when the value
    is absent or not a number."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _score(relevance: float, recency: float, confidence: float,
           importance: float) -> float:
    return relevance * recency * confidence * importance


def _fact_candidate(fact: dict, query: str, tokens: list[str], now: int) -> dict | None:
    text = str(fact.get("fact_text") or "").strip()
    if not text:
        return None
    entity = str(fact.get("entity_name") or "Entity")
    relevance = _relevance(query, tokens, f"{entity} {text}")
    # S6: the store's semantic attestation, when present, floors
    # the relevance factor (see the module docstring's S6
    # amendment) — max(lexical, semantic), never a replacement
    # ranking: the product formula still decides the order.
    semantic = fact.get("semantic_similarity")
    semantic_value: float | None = None
    if isinstance(semantic, (int, float)) and not isinstance(semantic, bool):
        semantic_value = min(1.0, max(0.0, float(semantic)))
        relevance = max(relevance, semantic_value)
    if tokens and relevance <= 0.0:
        return None
    confidence = _unit(fact.get("confidence"), 0.5)
    importance = _unit(fact.get("importance"), 0.5)
    recency = _recency(fact.get("timestamp"), now)
    fact_id = fact.get("fact_id") or fact.get("id") or "unknown"
    return {
        "source": "fact",
        "id": str(fact_id),
        "text": text,
        "score": _score(relevance, recency, confidence, importance),
        "relevance": relevance,
        "semantic_similarity": semantic_value,
        "recency": recency,
        "confidence": confidence,
        "importance": importance,
        "line": f"- {entity}: {text} [fact {fact_id}]",
    }


def _episode_candidate(episode: dict, query: str, tokens: list[str], now: int) -> dict | None:
    summary = str(episode.get("task_summary") or "").strip()
    prompt = str(episode.get("user_prompt") or "").strip()
    outcome = str(episode.get("outcome") or "").strip()
    relevance = _relevance(query, tokens, f"{prompt} {summary} {outcome}")
    if tokens and relevance <= 0.0:
        return None
    recency = _recency(episode.get("timestamp"), now)
    task_id = episode.get("task_id") or "unknown"
    shown_outcome = outcome[:160].replace("\n", " ")
    return {
        "source": "episode",
        "id": str(task_id),
        "text": summary or prompt,
        "score": _score(relevance, recency, 1.0, 0.5),
        "relevance": relevance,
        "recency": recency,
        "confidence": 1.0,
        "importance": 0.5,
        "line": f"- Past Task: '{prompt}' -> Outcome: '{shown_outcome}...' [episode {task_id}]",
    }


_CURATED_CONFIDENCE_RE = re.compile(r"\s*\(confidence ([0-9.]+)\)\s*$")


def _curated_candidates(query: str, tokens: list[str], now: int,
                        *, path: Path | None = None) -> list[dict]:
    """Parse the curated document's fact lines into candidates.

    Best-effort by design: a missing/unreadable document yields no
    candidates and never breaks assembly (the document is a weekly
    rendering; the live stores are the truth). Only the facts
    section is parsed — the document's episode lines duplicate the
    episodic store, which the assembler already reads directly.
    """
    from aether_engine.memory import consolidation

    target = path if path is not None else consolidation.MEMORY_DOCUMENT_PATH
    try:
        raw = Path(target).read_text(encoding="utf-8")
        mtime = int(Path(target).stat().st_mtime)
    except OSError:
        return []
    candidates: list[dict] = []
    in_facts = False
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if line.startswith("## "):
            in_facts = "knows" in line
            continue
        if line.startswith("## Recent tasks"):
            in_facts = False
            continue
        if not in_facts or not line.startswith("- "):
            continue
        text = line[2:].strip()
        confidence = 0.5
        match = _CURATED_CONFIDENCE_RE.search(text)
        if match:
            confidence = _unit(match.group(1), 0.5)
            text = text[: match.start()].strip()
        if not text or text.startswith("_"):
            continue
        relevance = _relevance(query, tokens, text)
        if tokens and relevance <= 0.0:
            continue
        recency = _recency(mtime, now)
        candidates.append({
            "source": "curated",
            "id": f"curated_memory.md:{lineno}",
            "text": text,
            "score": _score(relevance, recency, confidence, 0.5),
            "relevance": relevance,
            "recency": recency,
            "confidence": confidence,
            "importance": 0.5,
            "line": f"- {text} [curated_memory.md:{lineno}]",
        })
    return candidates


def _normalised(text: str) -> str:
    return " ".join((text or "").casefold().split())


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def assemble_memory_context(
    backend,
    query: str,
    *,
    config=None,
    budget_tokens: int | None = None,
    now: int | None = None,
    include_facts: bool = True,
    include_episodes: bool = True,
    include_curated: bool = True,
    curated_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble one scored memory briefing through the facade.

    ``backend`` is a facade MemoryBackend (see facade.py) — this
    module never talks to a store directly. Returns a dict:
    ``text`` (the briefing, "" when nothing scored in), ``items``
    (the included candidates, best first, each with source, id,
    score and its four factors), ``budget_tokens``, and
    ``estimated_tokens`` (the house estimate of ``text`` — never
    above the budget).

    Every section is best-effort: a backend method that is missing
    or fails contributes no candidates and is logged, mirroring the
    memory layer's degradation contract — assembly must never break
    a chat or a routing decision.
    """
    if budget_tokens is None:
        budget_tokens = int(getattr(config, "memory_context_token_budget", 1500) or 1500)
    budget_tokens = max(0, int(budget_tokens))
    now = int(time.time()) if now is None else int(now)
    tokens = _query_tokens(query)
    candidates: list[dict] = []

    if include_facts and backend is not None:
        try:
            # S6: prefer the backend's hybrid search (substring
            # priority + semantic recall) when it offers one, so
            # paraphrased queries reach candidates the substring
            # leg alone would never surface; backends and test
            # doubles without it keep the plain search. Either
            # way the candidates are scored by the one formula
            # above — retrieval decides candidacy, scoring ranks.
            hybrid = getattr(backend, "search_facts_hybrid", None)
            if hybrid is not None:
                facts = await _maybe_await(
                    hybrid(query, limit=FACT_CANDIDATE_LIMIT))
            else:
                facts = await _maybe_await(
                    backend.search_facts(query, limit=FACT_CANDIDATE_LIMIT))
        except Exception:  # one failing section must not sink assembly
            logger.warning("Context assembly: fact search failed", exc_info=True)
            facts = []
        if not facts:
            # Even the hybrid can come back empty (embeddings
            # unavailable AND no substring hit): an empty result
            # can mean "phrased differently", not "nothing
            # known". Fall back to the backend's top facts and let
            # the uniform relevance score judge them.
            try:
                facts = await _maybe_await(
                    backend.list_facts(limit=FACT_CANDIDATE_LIMIT))
            except Exception:  # best-effort section
                logger.warning("Context assembly: fact list failed", exc_info=True)
                facts = []
        for fact in facts or []:
            candidate = _fact_candidate(fact, query, tokens, now)
            if candidate is not None:
                candidates.append(candidate)

    if include_episodes and backend is not None:
        try:
            episodes = await _maybe_await(
                backend.search_episodes(query, limit=EPISODE_CANDIDATE_LIMIT))
        except Exception:  # best-effort section; some backends/doubles have no episode search
            logger.warning("Context assembly: episode search failed", exc_info=True)
            episodes = []
        for episode in episodes or []:
            candidate = _episode_candidate(episode, query, tokens, now)
            if candidate is not None:
                candidates.append(candidate)

    if include_curated:
        curated = _curated_candidates(query, tokens, now, path=curated_path)
        live_fact_texts = {
            _normalised(c["text"]) for c in candidates if c["source"] == "fact"
        }
        for candidate in curated:
            # A curated line that restates a live fact is a
            # duplicate: the live record wins (real id, fresh
            # factors). Containment both ways — the curated line
            # may carry the document's own trimming.
            normalised = _normalised(candidate["text"])
            if any(
                normalised and (normalised in live or live in normalised)
                for live in live_fact_texts
            ):
                continue
            candidates.append(candidate)

    candidates.sort(key=lambda c: (-c["score"], -c["recency"], c["source"], c["id"]))

    # Greedy budget assembly: per-line estimates include the join
    # newline (+1 char), and the sum of per-line ceilings bounds
    # the whole text's estimate, so the result can never exceed
    # the budget.
    used = estimate_tokens(_HEADER) + 1
    lines: list[str] = []
    items: list[dict] = []
    for candidate in candidates:
        cost = estimate_tokens(candidate["line"] + "\n")
        if used + cost > budget_tokens:
            continue
        used += cost
        lines.append(candidate["line"])
        items.append(candidate)

    if not lines:
        return {
            "text": "",
            "items": [],
            "budget_tokens": budget_tokens,
            "estimated_tokens": 0,
        }
    text = _HEADER + "\n" + "\n".join(lines)
    return {
        "text": text,
        "items": items,
        "budget_tokens": budget_tokens,
        "estimated_tokens": estimate_tokens(text),
    }


# ---------------------------------------------------------------------------
# Wave 6 (SPEC-6B C6): memory-used disclosure shaping.
#
# Both chat paths report which memory items actually scored into
# a reply's context, as ``memory_used`` entries on the reply:
# ``{id, summary, kind}`` with kind fact | episode | profile —
# IDs and one-line summaries only (SPEC-6B Rule 3), never raw
# stored text beyond the summary line. The shaping lives here,
# beside the assembler whose items it shapes, so the supervisor
# and fast-chat paths can never drift into two dialects.
#
# Two sources are deliberately NOT disclosed: the assembler's
# "curated" candidates (lines of the derived consolidation
# document — they restate stored facts, and the contract's kind
# vocabulary has no honest bucket for a document line) and the
# learned.md lessons (no per-item identity exists to name).
# ---------------------------------------------------------------------------

# A summary is one line of a chat affordance, not the record:
# long enough to recognise the item, short enough to scan.
_DISCLOSURE_SUMMARY_MAX_CHARS = 120

# SPEC-6B C6: a reply discloses at most this many items.
MEMORY_USED_LIMIT = 5


def _summary_line(text: Any) -> str:
    """One line, whitespace-collapsed, capped — the disclosure
    form of a stored item's text."""
    line = " ".join(str(text or "").split())
    if len(line) > _DISCLOSURE_SUMMARY_MAX_CHARS:
        line = line[: _DISCLOSURE_SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return line


def memory_used_entries(
    items: list[dict] | None, *, limit: int = MEMORY_USED_LIMIT
) -> list[dict[str, Any]]:
    """Disclosure entries for assembler ``items`` (the scored
    candidates assemble_memory_context returned), best first —
    the order the assembler ranked them. Fact and episode items
    only; curated lines are skipped (see the section comment).
    Deduped by (kind, id)."""
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        source = item.get("source")
        if source not in ("fact", "episode"):
            continue
        item_id = str(item.get("id") or "")
        summary = _summary_line(item.get("text"))
        if not item_id or not summary:
            continue
        key = (source, item_id)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"id": item_id, "summary": summary, "kind": source})
        if len(entries) >= limit:
            break
    return entries


def profile_used_entries(
    profile: dict | None, *, limit: int = MEMORY_USED_LIMIT
) -> list[dict[str, Any]]:
    """Disclosure entries for the profile-digest items of a
    facade ``user_profile()`` view: the user facts and
    preferences the digest renders (its top-3 user items and
    top-2 preferences — the same shown sets fast chat's digest
    builder uses). The digest's people line carries names only,
    no item text, so people items are not disclosed here.
    Deduped by id."""
    if not profile:
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    shown = list(profile.get("user") or [])[:3] + \
        list(profile.get("preferences") or [])[:2]
    for item in shown:
        fact_id = str(item.get("fact_id") or item.get("id") or "")
        summary = _summary_line(item.get("text"))
        if not fact_id or not summary or fact_id in seen:
            continue
        seen.add(fact_id)
        entries.append(
            {"id": fact_id, "summary": summary, "kind": "profile"})
        if len(entries) >= limit:
            break
    return entries
