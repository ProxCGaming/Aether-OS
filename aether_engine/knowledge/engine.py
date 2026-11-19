"""Query engine for knowledge collections (P4d, steps 1–2).

One collection, one hybrid retriever: a vector leg over the
persisted ``VectorStoreIndex`` and a BM25 leg over the same nodes,
merged by LlamaIndex's ``QueryFusionRetriever`` in reciprocal-rank
mode. Fusion runs with ``num_queries=1`` — the query is never
rewritten or expanded by an LLM, so search makes no cloud call of
its own; the only model involved is the configured local-first
embedder. (The reranker stage the P4d design names is step 4 of
the plan — K-13 — and slots into :meth:`KnowledgeBase.search`
behind this same outcome shape.)

Honesty rules, mirroring ``workspace_search``:

* When the embedding endpoint is down, the vector leg is skipped,
  BM25 still answers, and the outcome says so (``degraded``).
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
from aether_engine.memory import episodic

logger = logging.getLogger("aether_engine.knowledge.engine")

#: How many candidates each leg contributes before fusion.
LEG_TOP_K = 10


@dataclass
class KnowledgeHit:
    """One retrieved passage with its citation."""

    text: str
    score: float
    collection: str
    source_path: str  # collection-relative path of the source document
    chunk_index: int | None = None
    start_char: int | None = None
    end_char: int | None = None
    page_label: str | None = None


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


class CollectionNotIndexedError(RuntimeError):
    """The collection directory holds no index (no manifest)."""


class KnowledgeBase:
    """A loaded knowledge collection: manifest + lazy hybrid search."""

    def __init__(
        self,
        collection_dir: str | Path,
        *,
        config: UserConfig | None = None,
    ) -> None:
        self.collection_dir = Path(collection_dir).expanduser()
        self._config = config
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

    async def search(self, query: str, *, top_k: int = 5) -> SearchOutcome:
        """Hybrid (vector + BM25, RRF-fused) search over the collection."""
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
        if query_vector is None:
            outcome.degraded = True
            scored_nodes = await bm25.aretrieve(query)
        else:
            vector = VectorIndexRetriever(index=index, similarity_top_k=leg_k)
            # MockLLM only satisfies the fusion retriever's
            # constructor: with num_queries=1 no query generation
            # happens and the LLM is never called.
            fusion = QueryFusionRetriever(
                [vector, bm25],
                llm=MockLLM(),
                mode="reciprocal_rerank",
                num_queries=1,
                similarity_top_k=top_k,
                use_async=True,
            )
            scored_nodes = await fusion.aretrieve(query)

        for scored in scored_nodes[:top_k]:
            node = scored.node
            metadata = node.metadata or {}
            outcome.hits.append(KnowledgeHit(
                text=node.get_content(metadata_mode="none"),
                score=float(scored.score or 0.0),
                collection=self.name,
                source_path=str(metadata.get("source_path")
                                or metadata.get("file_name") or "unknown"),
                chunk_index=metadata.get("chunk_index"),
                start_char=node.start_char_idx,
                end_char=node.end_char_idx,
                page_label=metadata.get("page_label"),
            ))
        return outcome
