"""Reranker stage for knowledge search (P4d, step 4).

The P4d pipeline is hybrid retrieval (vector + BM25, reciprocal-
rank fusion) **plus a reranker** over the fused candidate pool.
Rank fusion is deliberately lossy — it keeps each leg's ordering
and throws away the scores' magnitudes — so a second pass re-scores
the pool by a direct relevance signal before the top-k cut.

The reranker here is **local-first and download-free**, per the
plan's embeddings/rerank rule and the same lesson as the chunker
(see ``knowledge.ingest``): a cross-encoder reranker
(sentence-transformers + weights) would download a model at first
use, and an LLM reranker would make search call the cloud — both
are out. Until P3b's local model tier can serve a cross-encoder,
the reranker re-scores candidates by **cosine similarity between
the query vector and each chunk's stored embedding** (the full
vectors the index already holds, including chunks the BM25 leg
surfaced that the vector leg never scored).

The two signals are combined by **reciprocal-rank fusion**, not
by blending raw scores: RRF magnitudes and cosine magnitudes are
not calibrated against each other, so each candidate is ranked
twice — once by the fusion order it arrived in, once by direct
cosine — and the ranks are fused:

    final = 1 / (60 + fusion_rank) + 1 / (60 + cosine_rank)

(Measured, honestly: on the programme's offline eval harness
this reranker still degrades retrieval — hit@3 1.00 → 0.95,
MRR@3 0.942 → 0.867; an earlier magnitude-blend form was worse,
0.95 / 0.783 — because the harness's deterministic fake embedder
makes cosine ranks noise for keyword-precise questions, and the
fusion it would overrule is carried by BM25. That is a property
of the harness embedder, not a measurement of the real one — but
it is the only measurement this environment offers, so the stage
ships **opt-in** (``UserConfig.knowledge_rerank``, default off)
rather than silently lowering the bar production search is held
to. Full numbers: tests/test_p4d_collections_rerank.py.)

The stage is a seam, not a hard-coding: :class:`KnowledgeBase`
takes any reranker callable with this module's signature, so a
cross-encoder served locally can replace the default without
touching search. When no candidate's vector can be resolved (or
the embedder is down, so there is no query vector), the reranker
does not run and the outcome says so — the fused order stands,
never a silently invented one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Name recorded on the search outcome when this reranker ran.
RERANKER_NAME = "embedding-cosine"

#: Width of the fused candidate pool the reranker re-scores.
RERANK_CANDIDATES = 20

#: Reciprocal-rank constant for the rerank fusion (the standard
#: RRF k; see module docstring).
RRF_K = 60


@dataclass
class RerankedNode:
    """One candidate after reranking."""

    node: Any
    score: float  # the rerank score the hit is ordered by
    retrieval_score: float  # the pre-rerank fusion score


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _candidate_vector(scored: Any, embeddings: dict | None) -> list[float] | None:
    """A candidate's stored vector, wherever it lives.

    Retrieved nodes do not reliably carry their embedding on the
    node object — a persisted index keeps vectors in its vector
    store, keyed by node id — so the caller passes that store's
    vectors in ``embeddings``; a vector on the node itself (a
    freshly built index) is honoured too.
    """
    if embeddings:
        vector = embeddings.get(getattr(scored.node, "node_id", None))
        if vector:
            return list(vector)
    embedding = getattr(scored.node, "embedding", None)
    return list(embedding) if embedding else None


def rerank_scored_nodes(
    query_vector: list[float],
    scored_nodes: list,
    *,
    top_n: int,
    embeddings: dict | None = None,
) -> tuple[list[RerankedNode], bool]:
    """Re-score fused candidates against the query vector.

    Returns ``(reranked, applied)``: ``applied`` is False — and the
    returned list empty — when no candidate's stored vector can be
    resolved, in which case the caller keeps the fused order
    untouched. Candidates without a resolvable vector are re-scored
    on the fusion component alone (cosine treated as 0); with at
    least one resolved vector the stage counts as applied.
    """
    if not scored_nodes or not query_vector:
        return [], False
    vectors = [_candidate_vector(sn, embeddings) for sn in scored_nodes]
    if not any(vectors):
        return [], False

    # Candidates arrive in fusion order, so their position IS
    # their fusion rank. Cosine ranks come from sorting the
    # resolved vectors' similarities (stable: ties keep the
    # fusion order). A candidate with no resolvable vector gets
    # no cosine term — it can only be carried by its fusion rank.
    cosines = [
        _cosine(query_vector, vector) if vector else None
        for vector in vectors
    ]
    cosine_rank: dict[int, int] = {}
    by_cosine = sorted(
        (i for i, c in enumerate(cosines) if c is not None),
        key=lambda i: cosines[i], reverse=True)
    for rank, i in enumerate(by_cosine, start=1):
        cosine_rank[i] = rank

    reranked: list[RerankedNode] = []
    for i, scored in enumerate(scored_nodes):
        final = 1.0 / (RRF_K + i + 1)
        if i in cosine_rank:
            final += 1.0 / (RRF_K + cosine_rank[i])
        reranked.append(RerankedNode(
            node=scored.node, score=final,
            retrieval_score=float(scored.score or 0.0)))
    # Stable sort: equal rerank scores keep the fused order.
    reranked.sort(key=lambda r: r.score, reverse=True)
    return reranked[: max(1, top_n)], True
