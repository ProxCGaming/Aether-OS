"""``search_knowledge_base`` tool (P4d, steps 1–2).

Deliberate retrieval over the owner's knowledge collections —
named folders of documents (PDF, Markdown, text, DOCX, code)
indexed by ``aether_engine.knowledge`` — returning passages with
source citations (collection + document + chunk position) and the
collection's last-indexed time, so a stale answer is detectable.

READ_ONLY and self-limiting, like ``workspace_search``: with no
collection indexed it answers "nothing indexed" instead of
failing, and it never invents hits. An index built by a different
embedder than the configured one is reported as needing a
re-index, never silently searched.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aether_engine.config import UserConfig
from aether_engine.knowledge.engine import (
    CollectionNotIndexedError,
    KnowledgeBase,
    SearchOutcome,
)
from aether_engine.knowledge.ingest import (
    EmbedderMismatchError,
    load_manifest,
)
from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.tools.knowledge_search")

_SNIPPET_CHARS = 1200
_DEFAULT_LIMIT = 5

_bases: dict[str, KnowledgeBase] = {}


def default_knowledge_dir() -> Path:
    """Where collections live: ``~/.aether/knowledge/<collection>/``."""
    return Path.home() / ".aether" / "knowledge"


def available_collections(knowledge_dir: Path | None = None) -> list[str]:
    """Names of collections that hold an index, sorted."""
    root = knowledge_dir or default_knowledge_dir()
    if not root.is_dir():
        return []
    return sorted(
        child.name for child in root.iterdir()
        if child.is_dir() and load_manifest(child) is not None)


def clear_knowledge_base_cache() -> None:
    """Drop cached KnowledgeBase objects (tests, re-index hooks)."""
    _bases.clear()


def _get_base(collection_dir: Path, config: UserConfig | None) -> KnowledgeBase:
    key = str(collection_dir)
    base = _bases.get(key)
    if base is None:
        base = KnowledgeBase(collection_dir, config=config)
        _bases[key] = base
    return base


def format_knowledge_outcome(query: str, outcome: SearchOutcome) -> str:
    """Render a search outcome as the tool's text for the agent."""
    if outcome.chunk_count == 0:
        return (
            f"The knowledge collection {outcome.collection!r} has been "
            f"indexed but contains no passages yet, so there is nothing "
            f"to search.")
    if not outcome.hits:
        return (
            f"No matches in the knowledge collection "
            f"{outcome.collection!r} for {query!r}.")
    header = (
        f"{len(outcome.hits)} result(s) from knowledge collection "
        f"{outcome.collection!r} (last indexed {outcome.last_indexed}):")
    lines = [header]
    if outcome.degraded:
        lines.append(
            "(Semantic ranking is unavailable — the embedding endpoint "
            "is down — so these are lexical matches only.)")
    for i, hit in enumerate(outcome.hits, start=1):
        snippet = hit.text
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS] + "\n…[snippet truncated]"
        location = f"chunk {hit.chunk_index}" if hit.chunk_index is not None else "chunk ?"
        if hit.start_char is not None and hit.end_char is not None:
            location += f", chars {hit.start_char}–{hit.end_char}"
        if hit.page_label:
            location += f", page {hit.page_label}"
        lines.append(
            f"\n[{i}] {hit.collection}/{hit.source_path} ({location})\n{snippet}")
    return "\n".join(lines)


async def search_knowledge_base(
    query: str,
    collection: str | None = None,
    *,
    limit: int = _DEFAULT_LIMIT,
    knowledge_dir: Path | None = None,
    config: UserConfig | None = None,
) -> str:
    """Search a knowledge collection; returns the formatted result text.

    ``collection`` names a collection under the knowledge dir. When
    omitted, the ``default`` collection is used if one exists, else
    the only collection; with several and no default, the caller is
    told the names and must choose.
    """
    root = knowledge_dir or default_knowledge_dir()
    names = available_collections(root)
    if not names:
        return (
            "No knowledge base has been indexed yet, so there is "
            "nothing to search. Knowledge collections are folders of "
            "documents (PDF, Markdown, text, DOCX, code) the owner "
            "indexes into ~/.aether/knowledge; none exist yet.")
    if collection is None:
        if "default" in names:
            collection = "default"
        elif len(names) == 1:
            collection = names[0]
        else:
            return (
                "Several knowledge collections exist — name one with "
                f"the collection argument: {', '.join(names)}.")
    elif collection not in names:
        return (
            f"There is no knowledge collection named {collection!r}. "
            f"Indexed collections: {', '.join(names)}.")
    try:
        base = _get_base(root / collection, config)
        outcome = await base.search(query, top_k=limit)
    except CollectionNotIndexedError:
        return (
            f"The knowledge collection {collection!r} has not been "
            f"indexed yet, so there is nothing to search.")
    except EmbedderMismatchError as exc:
        return str(exc)
    return format_knowledge_outcome(query, outcome)


async def _search_knowledge_base_fn(
    query: str,
    collection: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> str:
    return await search_knowledge_base(query, collection, limit=limit)


def create_search_knowledge_base_tool() -> Tool:
    return Tool(
        name="search_knowledge_base",
        description=(
            "Search the owner's knowledge base — documents the owner "
            "has indexed into collections (PDFs, notes, Markdown, "
            "DOCX, text and code files) — and get back the most "
            "relevant passages with source citations and the "
            "collection's last-indexed time. Use this for questions "
            "the owner's own documents answer. Workspace files are "
            "covered by workspace_search instead; this tool searches "
            "only indexed knowledge collections."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for in the knowledge base",
                },
                "collection": {
                    "type": "string",
                    "description": (
                        "Collection to search; omit for the default "
                        "collection"),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum passages to return (default 5)",
                },
            },
            "required": ["query"],
        },
        execute_fn=_search_knowledge_base_fn,
        risk=ToolRisk.READ_ONLY,
    )
