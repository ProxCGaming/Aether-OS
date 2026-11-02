"""``workspace_search`` tool (Epic 8, E8-3.2; grill Q15/Q16).

Deliberate retrieval over the workspace prose index: the agent
asks, the index answers with chunks carrying file-and-line
citations. Citations are verified against the live file before
being returned — the index is a pointer layer, the file on disk
stays the ground truth, and a span that no longer matches is
flagged, never silently trusted.

Ranking mirrors episodic memory's hybrid: a lexical leg (token
overlap) and a semantic leg (cosine over the unified embedder's
vectors) merged with RRF. When the embedding endpoint is down the
semantic leg is skipped and the result says so (``degraded``) —
lexical matches are still real matches. When nothing is indexed,
the tool says that plainly instead of inventing hits.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from aether_engine.config import UserConfig
from aether_engine.memory import episodic
from aether_engine.rag.prose_indexer import get_prose_indexer
from aether_engine.rag.store import WorkspaceRagStore
from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.tools.workspace_search")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RRF_K = 60
_MAX_LIMIT = 20
_SNIPPET_CHARS = 1200


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _lexical_score(query_tokens: set[str], content: str) -> int:
    """Distinct query tokens present in the chunk (the lexical leg)."""
    if not query_tokens:
        return 0
    return len(query_tokens & set(_tokens(content)))


def _verify_citation(root: str, rel_path: str, line_start: int,
                     line_end: int, content: str) -> tuple[bool, str]:
    """Re-read the cited span from the live file.

    Returns (verified, text): when the file's lines at the span are
    exactly the chunk content, the citation is verified and the
    file's own text is what gets cited. A vanished file reports
    (False, "") so the caller drops the stale hit.
    """
    path = Path(root) / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False, ""
    lines = text.split("\n")
    if line_start < 1 or line_end > len(lines) or line_start > line_end:
        return False, content
    span = "\n".join(lines[line_start - 1:line_end])
    return (span == content), span if span == content else content


async def search_workspace(
    query: str,
    *,
    limit: int = 5,
    workspace_roots: list[str] | None = None,
    store: WorkspaceRagStore,
    config: UserConfig | None = None,
) -> dict[str, Any]:
    """Search the prose index. Returns ``{results, degraded,
    indexed_roots}`` — results carry verified file+line citations.

    ``workspace_roots`` scopes the search to the granted roots the
    caller passes (the tool injects the run's roots); ``None``
    searches every indexed root in the store.
    """
    limit = max(1, min(int(limit or 5), _MAX_LIMIT))
    roots = [str(Path(r).expanduser()) for r in workspace_roots] if workspace_roots else None
    candidates = store.search_candidates(roots)
    indexed_roots = sorted({c["workspace_root"] for c in candidates})
    if not candidates:
        return {"results": [], "degraded": False, "indexed_roots": indexed_roots}

    query_tokens = set(_tokens(query))
    query_vec = await episodic.embed_text(
        query, config=config, context="workspace_search")
    degraded = query_vec is None

    lexical_ranked = sorted(
        (c for c in candidates if _lexical_score(query_tokens, c["content"]) > 0),
        key=lambda c: (-_lexical_score(query_tokens, c["content"]),
                       c["file_path"], c["line_start"]))
    semantic_ranked: list[dict[str, Any]] = []
    if query_vec is not None:
        scored = []
        for c in candidates:
            if c["embedding"] is None:
                continue
            sim = episodic._cosine_similarity(query_vec, c["embedding"])
            if sim > 0:
                scored.append((sim, c))
        semantic_ranked = [c for _sim, c in sorted(
            scored, key=lambda p: (-p[0], p[1]["file_path"], p[1]["line_start"]))]

    rrf: dict[str, dict[str, Any]] = {}
    for leg in (lexical_ranked, semantic_ranked):
        for rank, c in enumerate(leg):
            entry = rrf.setdefault(c["id"], {"chunk": c, "score": 0.0})
            entry["score"] += 1.0 / (_RRF_K + rank + 1)
    ordered = sorted(
        rrf.values(),
        key=lambda e: (-e["score"], e["chunk"]["file_path"], e["chunk"]["line_start"]))

    results: list[dict[str, Any]] = []
    for entry in ordered:
        if len(results) >= limit:
            break
        c = entry["chunk"]
        verified, text = _verify_citation(
            c["workspace_root"], c["file_path"],
            c["line_start"], c["line_end"], c["content"])
        if not verified and text == "":
            continue  # file vanished since indexing — stale hit, drop it
        results.append({
            "file_path": c["file_path"],
            "workspace_root": c["workspace_root"],
            "line_start": c["line_start"],
            "line_end": c["line_end"],
            "content": text,
            "score": entry["score"],
            "verified": verified,
        })
    return {"results": results, "degraded": degraded,
            "indexed_roots": indexed_roots}


def format_search_results(query: str, outcome: dict[str, Any]) -> str:
    """Render a search outcome as the tool's text for the agent."""
    results = outcome["results"]
    if not outcome["indexed_roots"]:
        return (
            "No workspace prose index is available yet — no workspace "
            "has been indexed, so there is nothing to search. The index "
            "covers prose only (Markdown, docs, notes, text); code is "
            "covered by the repo map and the live file tools."
        )
    if not results:
        return f"No matches in the workspace prose index for {query!r}."
    lines = [f"{len(results)} result(s) from the workspace prose index:"]
    if outcome["degraded"]:
        lines.append(
            "(Semantic ranking is unavailable — the embedding endpoint "
            "is down — so these are lexical matches only.)")
    for i, r in enumerate(results, start=1):
        snippet = r["content"]
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS] + "\n…[snippet truncated]"
        mark = "" if r["verified"] else " [citation could not be re-verified — file changed since indexing]"
        lines.append(
            f"\n[{i}] {r['file_path']}:{r['line_start']}-{r['line_end']}"
            f" (workspace {r['workspace_root']}){mark}\n{snippet}")
    return "\n".join(lines)


async def _workspace_search_fn(query: str, limit: int = 5,
                               workspace_roots: list | None = None) -> str:
    indexer = get_prose_indexer()
    outcome = await search_workspace(
        query, limit=limit, workspace_roots=workspace_roots,
        store=indexer.store)
    return format_search_results(query, outcome)


def create_workspace_search_tool() -> Tool:
    return Tool(
        name="workspace_search",
        description=(
            "Search the workspace's indexed prose — documentation, "
            "READMEs, PRDs, notes and text files — and get back the "
            "most relevant passages with file-and-line citations. "
            "Use this for questions the project's documents answer "
            "(decisions, specs, guides, changelogs). Code is not in "
            "this index; use the repo map, read_file and search_text "
            "for code."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for in the workspace documents",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum passages to return (default 5)",
                },
            },
            "required": ["query"],
        },
        execute_fn=_workspace_search_fn,
        risk=ToolRisk.READ_ONLY,
        wants_workspace_roots=True,
    )
