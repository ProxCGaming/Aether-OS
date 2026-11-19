"""Knowledge base (P4d): LlamaIndex collections behind one tool.

Deliberately separate from ``aether_engine.rag`` (Epic 8): that
package indexes the *granted workspace's* prose for the
``workspace_search`` tool. This package indexes the owner's
*knowledge collections* — named folders of documents (PDF / MD /
TXT / DOCX / code) — behind the single ``search_knowledge_base``
tool. Both embed through the one configured local-first embedder
(``episodic.embed_text``); neither switches with the memory
backend.

Steps 1–2 of the plan live here: ingestion + persisted hybrid
index (:mod:`aether_engine.knowledge.ingest`) and the query engine
(:mod:`aether_engine.knowledge.engine`). Owner collection
management (add-folder flow, stats surfaces) is step 3; the
reranker stage is step 4.
"""

from aether_engine.knowledge.embeddings import (
    AetherEmbedding,
    EmbeddingUnavailableError,
)
from aether_engine.knowledge.engine import (
    CollectionNotIndexedError,
    KnowledgeBase,
    KnowledgeHit,
    SearchOutcome,
)
from aether_engine.knowledge.ingest import (
    EmbedderMismatchError,
    IndexStats,
    index_collection,
    load_manifest,
)

__all__ = [
    "AetherEmbedding",
    "CollectionNotIndexedError",
    "EmbedderMismatchError",
    "EmbeddingUnavailableError",
    "IndexStats",
    "KnowledgeBase",
    "KnowledgeHit",
    "SearchOutcome",
    "index_collection",
    "load_manifest",
]
