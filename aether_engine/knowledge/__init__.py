"""Knowledge base (P4d): LlamaIndex collections behind one tool.

Deliberately separate from ``aether_engine.rag`` (Epic 8): that
package indexes the *granted workspace's* prose for the
``workspace_search`` tool. This package indexes the owner's
*knowledge collections* — named folders of documents (PDF / MD /
TXT / DOCX / code) — behind the single ``search_knowledge_base``
tool. Both embed through the one configured local-first embedder
(``episodic.embed_text``); neither switches with the memory
backend.

All four steps of the plan live here: ingestion + persisted
hybrid index (:mod:`aether_engine.knowledge.ingest`) and the query
engine (:mod:`aether_engine.knowledge.engine`) from steps 1–2;
owner collection management — config, the add-folder flow,
incremental re-index jobs, stats — in
:mod:`aether_engine.knowledge.collections` (step 3); and the
local-first reranker stage in
:mod:`aether_engine.knowledge.rerank` (step 4).
"""

from aether_engine.knowledge.collections import (
    CollectionExistsError,
    CollectionInfo,
    UnknownCollectionError,
    add_folder,
    create_collection,
    get_collection,
    list_collections,
    reindex_collection,
    remove_collection,
    remove_folder,
)
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
from aether_engine.knowledge.rerank import RERANKER_NAME, rerank_scored_nodes

__all__ = [
    "RERANKER_NAME",
    "AetherEmbedding",
    "CollectionExistsError",
    "CollectionInfo",
    "CollectionNotIndexedError",
    "EmbedderMismatchError",
    "EmbeddingUnavailableError",
    "IndexStats",
    "KnowledgeBase",
    "KnowledgeHit",
    "SearchOutcome",
    "UnknownCollectionError",
    "add_folder",
    "create_collection",
    "get_collection",
    "index_collection",
    "list_collections",
    "load_manifest",
    "reindex_collection",
    "remove_collection",
    "remove_folder",
    "rerank_scored_nodes",
]
