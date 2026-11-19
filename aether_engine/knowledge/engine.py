"""Query engine for knowledge collections (P4d, steps 1–2 + 4).

One collection, one hybrid retriever: a vector leg over the
persisted ``VectorStoreIndex`` and a BM25 leg over the same nodes,
merged by LlamaIndex's ``QueryFusionRetriever`` in reciprocal-rank
mode. Fusion runs with ``num_queries=1`` — the query is never
rewritten or expanded by an LLM, so search makes no cloud call of
its own; the only model involved is the configured local-first
embedder.

When enabled, the fused pool then passes through the **reranker
stage** (step 4; :mod:`aether_engine.knowledge.rerank`): up to
``RERANK_CANDIDATES`` fused candidates are re-scored locally and
the top-k is cut from the reranked order. The stage is opt-in
(``UserConfig.knowledge_rerank``, default off — measured reason
recorded on that field and in the step-4 eval). Either way the
outcome records whether the reranker ran (``reranked`` /
``reranker``) and, when it did, each hit keeps its pre-rerank
fusion score (``retrieval_score``), so the stage's effect is
inspectable, never silent.

Honesty rules, mirroring ``workspace_search``:

* When the embedding endpoint is down, the vector leg is skipped,
  BM25 still answers, and the outcome says so (``degraded``) —
  and the reranker, which needs the query vector, does not run.
* Every hit carries its citation (collection + source path +
  chunk position) and the outcome carries the collection's
  last-indexed time, so a stale answer is detectable as stale.
* An index built by a different embedder than the configured one
  is refused (:class:`EmbedderMismatchError`), never silently
  searched across vector spaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig
from aether_engine.knowledge.embeddings import AetherEmbedding
from aether_engine.knowledge.ingest import check_embedder, load_manifest
from aether_engine.knowledge.rerank import (
    RERANK_CANDIDATES,
    RERANKER_NAME,
    RerankedNode,
    rerank_scored_nodes,
)
from aether_engine.memory import episodic

logger = logging.getLogger("aether_engine.knowledge.engine")

#: How many candidates each leg contributes before fusion.
LEG_TOP_K = 10


@dataclass
class KnowledgeHit:
    """One retrieved passage with its citation."""

    text: str
    score: float  # the ordering score (rerank score when reranked)
    collection: str
    source_path: str  # collection-relative path of the source document
    chunk_index: int | None = None
    start_char: int | None = None
    end_char: int | None = None
    page_label: str | None = None
    # The pre-rerank fusion score, set only when the reranker ran.
    retrieval_score: float | None = None


@dataclass
class SearchOutcome:
    """One search over one collection."""

    collection: str
    hits: list[KnowledgeHit] = field(default_factory=list)
    degraded: bool = False  # True when the vector leg was unavailable
    embedder: str = ""
    last_indexed: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    reranked: bool = False  # True when the reranker re-scored the pool
    reranker: str = ""  # name of the reranker that ran, when it did


class CollectionNotIndexedError(RuntimeError):
    """The collection directory holds no index (no manifest)."""


class KnowledgeBase:
    """A loaded knowledge collection: manifest + lazy hybrid search."""

    def __init__(
        self,
        collection_dir: str | Path,
        *,
        config: UserConfig | None = None,
        rerank: bool | None = None,
    ) -> None:
        self.collection_dir = Path(collection_dir).expanduser()
        self._config = config
        # The reranker stage (step 4) is opt-in: an explicit
        # argument wins, otherwise the owner's config decides
        # (UserConfig.knowledge_rerank, default off — the measured
        # reason is recorded on that field). The eval set's
        # before/after measurement passes the argument both ways.
        self._rerank = (
            rerank if rerank is not None
            else bool(getattr(config, "knowledge_rerank", False)))
        manifest = load_manifest(self.collection_dir)
        if manifest is None:
            raise CollectionNotIndexedError(
                f"no knowledge index at {self.collection_dir} — the "
                f"collection has not been indexed yet.")
        self._manifest = manifest
        # Refuse a cross-vector-space search up front.
        self.embedder = check_embedder(manifest, config=config)
        self._index: Any = None

    @property
    def name(self) -> str:
        return self.collection_dir.name

    def stats(self) -> dict[str, Any]:
        """Collection stats from the manifest (docs, chunks, when)."""
        return {
            "collection": self.name,
            "source_root": self._manifest.get("source_root"),
            "source_roots": self._manifest.get("source_roots"),
            "embedder": self._manifest.get("embedder"),
            "indexed_at": self._manifest.get("indexed_at"),
            "doc_count": int(self._manifest.get("doc_count") or 0),
            "chunk_count": int(self._manifest.get("chunk_count") or 0),
        }

    def _load_index(self) -> Any:
        if self._index is None:
            from llama_index.core import StorageContext, load_index_from_storage

            from aether_engine.knowledge.ingest import INDEX_SUBDIR

            persist_dir = self.collection_dir / INDEX_SUBDIR
            self._index = load_index_from_storage(
                StorageContext.from_defaults(persist_dir=str(persist_dir)),
                embed_model=AetherEmbedding(config=self._config))
        return self._index

    def _stored_vectors(self, scored_nodes: list) -> dict[str, list[float]]:
        """Chunk vectors from the index's vector store, by node id.

        Retrieved nodes arrive without their embeddings (the store
        keeps them separately), so the reranker is handed the
        store's own vectors. A store that cannot answer per-id
        lookups yields an empty map, and the reranker then honestly
        does not run.
        """
        get_vector = getattr(self._load_index().vector_store, "get", None)
        if not callable(get_vector):
            return {}
        vectors: dict[str, list[float]] = {}
        for scored in scored_nodes:
            node_id = getattr(scored.node, "node_id", None)
            if node_id is None:
                continue
            try:
                vector = get_vector(node_id)
            except KeyError:
                continue
            if vector:
                vectors[node_id] = list(vector)
        return vectors

    async def search(self, query: str, *, top_k: int = 5) -> SearchOutcome:
        """Hybrid search: vector + BM25, RRF-fused, then reranked.

        The reranker stage (step 4) runs only when enabled (see
        the constructor) and the embedder is up; the outcome's
        ``reranked`` flag always says which happened.
        """
        top_k = max(1, min(int(top_k or 5), 20))
        outcome = SearchOutcome(
            collection=self.name,
            embedder=self.embedder,
            last_indexed=self._manifest.get("indexed_at"),
            doc_count=int(self._manifest.get("doc_count") or 0),
            chunk_count=int(self._manifest.get("chunk_count") or 0),
        )
        if outcome.chunk_count == 0:
            return outcome
        index = self._load_index()

        from llama_index.core.llms.mock import MockLLM
        from llama_index.core.retrievers import (
            QueryFusionRetriever,
            VectorIndexRetriever,
        )
        from llama_index.retrievers.bm25 import BM25Retriever

        leg_k = max(top_k, LEG_TOP_K)
        bm25 = BM25Retriever.from_defaults(index=index, similarity_top_k=leg_k)

        # Probe the embedder the same way workspace_search does: a
        # None vector means the endpoint is down, so the vector leg
        # is skipped and the outcome is flagged degraded — BM25
        # matches are still real matches.
        query_vector = await episodic.embed_text(
            query, config=self._config, context="knowledge_search")
        # (node, ordering score, pre-rerank score or None) triples.
        ranked: list[tuple[Any, float, float | None]] = []
        if query_vector is None:
            outcome.degraded = True
            scored_nodes = await bm25.aretrieve(query)
            ranked = [(sn.node, float(sn.score or 0.0), None)
                      for sn in scored_nodes[:top_k]]
        else:
            vector = VectorIndexRetriever(index=index, similarity_top_k=leg_k)
            # With the reranker on, fusion returns the wider pool
            # the reranker re-scores; off, it cuts straight to top_k.
            pool_k = max(top_k, RERANK_CANDIDATES) if self._rerank else top_k
            # MockLLM only satisfies the fusion retriever's
            # constructor: with num_queries=1 no query generation
            # happens and the LLM is never called.
            fusion = QueryFusionRetriever(
                [vector, bm25],
                llm=MockLLM(),
                mode="reciprocal_rerank",
                num_queries=1,
                similarity_top_k=pool_k,
                use_async=True,
            )
            scored_nodes = await fusion.aretrieve(query)
            reranked_nodes: list[RerankedNode] = []
            applied = False
            if self._rerank:
                reranked_nodes, applied = rerank_scored_nodes(
                    query_vector, scored_nodes, top_n=top_k,
                    embeddings=self._stored_vectors(scored_nodes))
            if applied:
                outcome.reranked = True
                outcome.reranker = RERANKER_NAME
                ranked = [(r.node, r.score, r.retrieval_score)
                          for r in reranked_nodes]
            else:
                ranked = [(sn.node, float(sn.score or 0.0), None)
                          for sn in scored_nodes[:top_k]]

        for node, score, retrieval_score in ranked:
            metadata = node.metadata or {}
            outcome.hits.append(KnowledgeHit(
                text=node.get_content(metadata_mode="none"),
                score=score,
                collection=self.name,
                source_path=str(metadata.get("source_path")
                                or metadata.get("file_name") or "unknown"),
                chunk_index=metadata.get("chunk_index"),
                start_char=node.start_char_idx,
                end_char=node.end_char_idx,
                page_label=metadata.get("page_label"),
                retrieval_score=retrieval_score,
            ))
        return outcome
