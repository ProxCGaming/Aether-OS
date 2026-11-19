"""Task-based tool retrieval — P2c Filter 2 (orchestration programme).

Three filters decide what a specialist delegation can do with tools:

* **Filter 1 — the per-agent ceiling** (`tools/ceilings.py`): a hard
  safety bound on what may be offered, applied first.
* **Filter 2 — this module.** Inside the Filter-1 set only, rank the
  tools against the task briefing and offer the top-K plus the
  agent's always-pinned essentials. Retrieval may only ever NARROW
  the ceiling's output — it cannot widen it, and it never touches
  Filter 3.
* **Filter 3 — the policy/approval execution gate**
  (`tools/policy.py`, `tools/enforcement.py`), unchanged: it judges
  each call at execution time, offered or not.

**Scorer.** The index interface (`ToolIndex.search`) hides which
scorer ranks the tools. This slice ships the lexical BM25 scorer:
the programme's embedding model (P3b's set) is not in the engine
yet, and the plan's instruction is BM25 first in exactly that case.
An embedding scorer can replace `ToolIndex`'s internals later
without any caller changing. The index is rebuilt from the offered
definitions on each delegation — tools are code, not user data, so
there is nothing to persist and nothing to drift.

**When it runs.** `UserConfig.tool_retrieval` is "off" by default —
today's behaviour exactly: the Filter-1 set is offered whole. In
"auto", retrieval narrows the offer per delegation. P1e's settings
surface (tool access mode per agent + top-K) is the intended owner
of this switch; until then it is a config field like S14's
`model_routing`, which this module's mode reader mirrors.

**Confidence fallback — retrieval must never starve an agent.**
`apply_tool_retrieval` returns the full input set unchanged when:

* the input set is small (≤ K) — narrowing a set that already fits
  buys nothing and risks a miss; or
* retrieval has no signal — no tool scores above zero against the
  briefing (no shared vocabulary at all), so any pick would be a
  guess.

**Always kept, in addition to the top-K:** the agent's pinned
essentials (below), and any tool already used in this conversation
— the same in-flight-integrity rule Filter 1 applies: a conversation
that legitimately used a tool never loses it mid-flight.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from aether_engine.tools.registry import ToolRisk

#: Top-K bounds from the plan: default 10, configurable range 8–12.
DEFAULT_TOP_K = 10
MIN_TOP_K = 8
MAX_TOP_K = 12

#: Always-pinned essentials per agent: the tools that define the
#: role, offered in "auto" mode whatever the briefing scores. Kept
#: deliberately minimal — pinning everything would defeat retrieval;
#: pinning nothing risks the silent-miss failure mode the plan calls
#: out. An agent with no entry has no pins.
AGENT_PINNED_TOOLS: dict[str, frozenset[str]] = {
    "researcher": frozenset({"search_web", "fetch_url", "read_file"}),
    "planner": frozenset({"scratchpad_render", "read_file"}),
    "coder": frozenset(
        {"read_file", "write_file", "edit_file", "execute_shell"}),
}


def tool_retrieval_mode(user_config: Any | None) -> str:
    """The configured retrieval mode: "auto" only when exactly that.

    Mirrors S14's `model_routing.routing_mode` reader so the two
    opt-in delegation behaviours read their config the same way.
    """
    mode = getattr(user_config, "tool_retrieval", "off")
    return "auto" if mode == "auto" else "off"


def tool_retrieval_top_k(user_config: Any | None) -> int:
    """The configured top-K, clamped to the plan's 8–12 range."""
    raw = getattr(user_config, "tool_retrieval_top_k", DEFAULT_TOP_K)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(MIN_TOP_K, min(MAX_TOP_K, value))


def pinned_for(agent: str) -> frozenset[str]:
    """The agent's always-pinned tool names (empty when none)."""
    return AGENT_PINNED_TOOLS.get(agent, frozenset())


_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Function words the tool descriptions are full of ("Use when the
#: user asks to …") — they carry no relevance signal and would
#: otherwise dominate a lexical score.
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "for", "on", "in", "is", "it",
    "when", "use", "used", "user", "asks", "ask", "with", "and",
    "or", "by", "from", "this", "that", "one", "its", "as", "at",
})


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower())
            if len(t) >= 2 and t not in _STOPWORDS]


@dataclass(frozen=True)
class ToolDocument:
    """One indexed tool: the plan's index record — name, description,
    category (its `ToolRisk` class) — plus its searchable tokens."""

    name: str
    description: str
    risk: ToolRisk
    tokens: tuple[str, ...]


class ToolIndex:
    """A searchable index over a set of tool definitions.

    The interface hides the scorer: `search` returns (name, score)
    pairs, best first, for the candidate names it is given. This
    implementation scores with BM25 over each tool's name +
    description (name tokens counted twice — a briefing that names
    the job usually names the tool's own vocabulary). Scores are
    >= 0; a tool sharing no token with the query scores exactly 0,
    which is the caller's no-signal test.
    """

    _K1 = 1.5
    _B = 0.75

    def __init__(self, documents: list[ToolDocument]) -> None:
        self._documents = documents
        self._term_freqs: list[Counter] = [
            Counter(d.tokens) for d in documents]
        self._doc_lens = [len(d.tokens) for d in documents]
        self._avg_len = (sum(self._doc_lens) / len(self._doc_lens)
                         if self._doc_lens else 0.0)
        doc_freq: Counter = Counter()
        for tf in self._term_freqs:
            doc_freq.update(tf.keys())
        n = len(documents)
        self._idf = {
            term: math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for term, df in doc_freq.items()
        }

    @classmethod
    def from_definitions(
        cls,
        definitions: list[dict[str, Any]],
        risk_by_name: dict[str, ToolRisk],
    ) -> ToolIndex:
        """Build the index from registry definition dicts. A tool
        whose risk is unknown is recorded as EXECUTE — the registry's
        fail-closed default — though retrieval only ranks; the risk
        is carried for inspection, never used to widen an offer."""
        documents = []
        for definition in definitions:
            fn = definition.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            description = fn.get("description") or ""
            tokens = tuple(_tokens(name) + _tokens(name)
                           + _tokens(description))
            documents.append(ToolDocument(
                name=name,
                description=description,
                risk=risk_by_name.get(name, ToolRisk.EXECUTE),
                tokens=tokens,
            ))
        return cls(documents)

    def search(
        self, query: str, candidates: Iterable[str],
    ) -> list[tuple[str, float]]:
        """BM25-score `candidates` against `query`, best first.

        Ties break by candidate order (the caller's offer order), so
        results are deterministic. Only candidates present in the
        index are returned."""
        wanted = list(candidates)
        position = {name: i for i, name in enumerate(wanted)}
        query_terms = set(_tokens(query))
        scored: list[tuple[str, float]] = []
        for doc, tf, doc_len in zip(
                self._documents, self._term_freqs, self._doc_lens):
            if doc.name not in position:
                continue
            score = 0.0
            for term in query_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                denom = freq + self._K1 * (
                    1.0 - self._B
                    + self._B * (doc_len / self._avg_len
                                 if self._avg_len else 1.0))
                score += self._idf.get(term, 0.0) * (
                    freq * (self._K1 + 1.0) / denom)
            scored.append((doc.name, score))
        scored.sort(key=lambda item: (-item[1], position[item[0]]))
        return scored


def apply_tool_retrieval(
    agent: str,
    definitions: list[dict[str, Any]],
    risk_by_name: dict[str, ToolRisk],
    *,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    pinned: frozenset[str] | set[str] | None = None,
    used_names: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Filter 2: narrow one specialist's offered definitions to the
    briefing's top-K, inside the set Filter 1 already allowed.

    The output is always a subset of the input, in the input's
    order, and the input is never mutated. See the module docstring
    for the fallback rules (small set / no signal → input unchanged)
    and the always-kept sets (pinned essentials, already-used tools)
    that ride in addition to the top-K.
    """
    k = max(MIN_TOP_K, min(MAX_TOP_K, int(top_k)))
    if len(definitions) <= k:
        return list(definitions)
    names = [(d.get("function") or {}).get("name") for d in definitions]
    index = ToolIndex.from_definitions(definitions, risk_by_name)
    scored = index.search(query, [n for n in names if n])
    if not scored or scored[0][1] <= 0.0:
        # No signal: the briefing shares no vocabulary with any tool
        # description. Guessing here would starve the agent — offer
        # the full Filter-1 set, per the plan's confidence fallback.
        return list(definitions)
    # `scored` is ranked best-first, so its head is the top-K; the
    # score filter drops any zero-signal tail the slice included.
    keep = {name for name, score in scored[:k] if score > 0.0}
    keep |= (pinned_for(agent) if pinned is None else set(pinned))
    keep |= set(used_names)
    return [d for d, name in zip(definitions, names)
            if name is None or name in keep]
