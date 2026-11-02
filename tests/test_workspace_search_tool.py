"""Epic 8, E8-3.2: the ``workspace_search`` tool.

Agents query the prose index deliberately and get chunks back with
file-and-line citations that resolve to the real text on disk — the
index is a pointer layer, live files stay the ground truth. Search
degrades honestly: endpoint down → lexical matches only, flagged;
no index → a clear "nothing indexed" answer, never invented hits.

Embeddings are the same deterministic fake as test_prose_indexer
(no live Ollama in this environment), patched over the shared
``litellm.aembedding`` seam.
"""
import math
import re
import time
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.memory import episodic
from aether_engine.rag.prose_indexer import ProseIndexer
from aether_engine.rag.store import WorkspaceRagStore
from aether_engine.tools.registry import ToolRisk, build_tool_registry
from aether_engine.tools.workspace_search import (
    create_workspace_search_tool,
    search_workspace,
)


def _vec(text: str, dims: int = 64) -> list[float]:
    v = [0.0] * dims
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[zlib.crc32(tok.encode()) % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _fake_aembedding(model=None, input=None, **_kwargs):
    resp = type("Resp", (), {})()
    resp.data = [{"embedding": _vec(t)} for t in (input or [])]
    return resp


@pytest.fixture(autouse=True)
def _reset_breaker():
    episodic._embed_unavailable_until = 0.0
    yield
    episodic._embed_unavailable_until = 0.0


@pytest.fixture
def store(tmp_path):
    return WorkspaceRagStore(tmp_path / "workspace_rag.db")


@pytest.fixture
def indexer(store):
    return ProseIndexer(store=store)


GUIDE = """# Configuration Guide

## Overview
This guide explains how the application is tuned.

## Parameters
Configuration parameters are defined in settings.toml.
Each configuration parameter has a name, a default, and a description.
"""

RECIPES = """# Recipes

## Bread
Mix flour, water, yeast and salt. Knead for ten minutes.
Bake at 220 degrees until the crust is golden.
"""


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


async def _indexed_workspace(indexer, tmp_path) -> Path:
    ws = tmp_path / "ws"
    _write(ws, "guide.md", GUIDE)
    _write(ws, "recipes.md", RECIPES)
    _write(ws, "app.py", "configuration = 'code is never indexed'\n")
    indexer.grant_consent(str(ws))
    with patch("aether_engine.memory.episodic.litellm.aembedding",
               new=AsyncMock(side_effect=_fake_aembedding)):
        await indexer.index_workspace(str(ws))
    return ws


# ----------------------------------------------------------------------
# Citations resolve to real text (the PRD acceptance shape)
# ----------------------------------------------------------------------

async def test_search_returns_verified_citations(indexer, store, tmp_path):
    ws = await _indexed_workspace(indexer, tmp_path)
    outcome = await search_workspace(
        "configuration parameters", store=store, workspace_roots=[str(ws)])
    results = outcome["results"]
    assert results, "expected at least one hit"
    top = results[0]
    assert top["file_path"] == "guide.md"
    assert top["verified"] is True
    # The cited span, read back from the real file, is the chunk text
    # and contains the queried phrase.
    lines = (ws / "guide.md").read_text(encoding="utf-8").split("\n")
    span = "\n".join(lines[top["line_start"] - 1:top["line_end"]])
    assert span == top["content"]
    assert "Configuration parameters" in span
    # Code was never indexed, so it can never be cited.
    assert all(r["file_path"] != "app.py" for r in results)


async def test_search_results_carry_line_numbers_within_file(indexer, store, tmp_path):
    ws = await _indexed_workspace(indexer, tmp_path)
    outcome = await search_workspace(
        "configuration parameters", store=store, workspace_roots=[str(ws)])
    total_lines = len(GUIDE.split("\n"))
    for r in outcome["results"]:
        assert 1 <= r["line_start"] <= r["line_end"] <= total_lines


# ----------------------------------------------------------------------
# The tool itself: registration, risk class, registry execution
# ----------------------------------------------------------------------

def test_tool_is_registered_read_only():
    tool = create_workspace_search_tool()
    assert tool.name == "workspace_search"
    assert tool.risk == ToolRisk.READ_ONLY
    registry = build_tool_registry()
    assert registry.get_tool("workspace_search").risk == ToolRisk.READ_ONLY
    assert "workspace_search" in registry.read_only_names()


async def test_tool_execution_through_registry(indexer, tmp_path):
    ws = await _indexed_workspace(indexer, tmp_path)
    registry = build_tool_registry()
    with patch("aether_engine.tools.workspace_search.get_prose_indexer",
               return_value=indexer):
        result = await registry.execute(
            "workspace_search",
            {"query": "configuration parameters"},
            workspace_roots=[str(ws)],
        )
    assert isinstance(result, str)
    assert re.search(r"guide\.md:\d+-\d+", result)
    assert "Configuration parameters" in result


# ----------------------------------------------------------------------
# Honest degradation
# ----------------------------------------------------------------------

async def test_search_degrades_to_lexical_when_endpoint_down(indexer, store, tmp_path):
    ws = await _indexed_workspace(indexer, tmp_path)
    # Breaker open (endpoint recently failed): the query embedding is
    # skipped, so search must say so and still answer lexically.
    episodic._embed_unavailable_until = time.monotonic() + 60.0
    outcome = await search_workspace(
        "configuration parameters", store=store, workspace_roots=[str(ws)])
    assert outcome["degraded"] is True
    assert outcome["results"], "lexical matches must survive the outage"
    assert outcome["results"][0]["file_path"] == "guide.md"


async def test_search_with_no_index_is_honest(store, tmp_path):
    # A granted root with nothing indexed yet: the honest answer is
    # "no index", distinct from the no-grant answer (S2-M2).
    granted = str(tmp_path / "ws")
    outcome = await search_workspace(
        "anything at all", store=store, workspace_roots=[granted])
    assert outcome["results"] == []
    assert outcome["indexed_roots"] == []
    tool = create_workspace_search_tool()
    with patch("aether_engine.tools.workspace_search.get_prose_indexer",
               return_value=ProseIndexer(store=store)):
        text = await tool.execute_fn(
            query="anything at all", workspace_roots=[granted])
    assert "No workspace prose index" in text


# ----------------------------------------------------------------------
# The index does NOT switch with the memory backend (fence, Q15–Q19)
# ----------------------------------------------------------------------

async def test_memory_backend_switch_leaves_index_searchable(indexer, store, tmp_path):
    ws = await _indexed_workspace(indexer, tmp_path)
    # Flip the configured memory backend: the workspace index is its
    # own store and must be untouched by the switch.
    other = ProseIndexer(
        store=store, config=UserConfig(memory_backend="mem0"))
    assert other.store is store
    outcome = await search_workspace(
        "configuration parameters", store=store, workspace_roots=[str(ws)],
        config=UserConfig(memory_backend="mem0"))
    assert outcome["results"]
    assert outcome["results"][0]["file_path"] == "guide.md"


# ----------------------------------------------------------------------
# Root scoping: a search only sees the granted roots it is given
# ----------------------------------------------------------------------

async def test_search_scopes_to_given_roots(indexer, store, tmp_path):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    _write(ws1, "guide.md", GUIDE)
    _write(ws2, "baking.md", RECIPES)
    for ws in (ws1, ws2):
        indexer.grant_consent(str(ws))
    with patch("aether_engine.memory.episodic.litellm.aembedding",
               new=AsyncMock(side_effect=_fake_aembedding)):
        await indexer.index_workspace(str(ws1))
        await indexer.index_workspace(str(ws2))

    only_ws2 = await search_workspace(
        "bread flour yeast", store=store, workspace_roots=[str(ws2)])
    assert only_ws2["results"]
    assert {r["workspace_root"] for r in only_ws2["results"]} == {str(ws2)}

    only_ws1 = await search_workspace(
        "bread flour yeast", store=store, workspace_roots=[str(ws1)])
    assert all(r["workspace_root"] == str(ws1) for r in only_ws1["results"])


# ----------------------------------------------------------------------
# S2-M2: NO granted roots = NO search at all (never unscoped)
#
# Regression: workspace_roots=None/[] used to fall through to an
# unscoped search over every indexed workspace in the store, leaking
# prose from workspaces the run was never granted.
# ----------------------------------------------------------------------

WS_A = """# Alpha Notes

The zebra protocol is documented here for the alpha workspace.
"""

WS_B = """# Beta Notes

The zebra protocol is documented here for the beta workspace.
"""


async def _indexed_two_workspaces(indexer, tmp_path):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    _write(ws1, "alpha.md", WS_A)
    _write(ws2, "beta.md", WS_B)
    for ws in (ws1, ws2):
        indexer.grant_consent(str(ws))
    with patch("aether_engine.memory.episodic.litellm.aembedding",
               new=AsyncMock(side_effect=_fake_aembedding)):
        await indexer.index_workspace(str(ws1))
        await indexer.index_workspace(str(ws2))
    return ws1, ws2


async def test_search_with_no_granted_roots_returns_nothing(
        indexer, store, tmp_path):
    await _indexed_two_workspaces(indexer, tmp_path)
    # Sanity: the content really is indexed and findable when granted.
    assert store.search_candidates(
        [str(tmp_path / "ws1"), str(tmp_path / "ws2")])
    for no_roots in (None, []):
        outcome = await search_workspace(
            "zebra protocol", store=store, workspace_roots=no_roots)
        assert outcome["results"] == []
        assert outcome["indexed_roots"] == []


async def test_store_search_candidates_with_no_roots_returns_nothing(
        indexer, store, tmp_path):
    await _indexed_two_workspaces(indexer, tmp_path)
    assert store.search_candidates(None) == []
    assert store.search_candidates([]) == []


async def test_search_with_one_granted_root_returns_only_that_root(
        indexer, store, tmp_path):
    ws1, ws2 = await _indexed_two_workspaces(indexer, tmp_path)
    for granted, other in ((ws1, ws2), (ws2, ws1)):
        outcome = await search_workspace(
            "zebra protocol", store=store, workspace_roots=[str(granted)])
        assert outcome["results"], "granted root's hits must be returned"
        assert {r["workspace_root"] for r in outcome["results"]} == {
            str(granted)}
        assert str(other) not in outcome["indexed_roots"]


async def test_search_with_both_granted_roots_returns_both(
        indexer, store, tmp_path):
    ws1, ws2 = await _indexed_two_workspaces(indexer, tmp_path)
    outcome = await search_workspace(
        "zebra protocol", store=store,
        workspace_roots=[str(ws1), str(ws2)])
    assert {r["workspace_root"] for r in outcome["results"]} == {
        str(ws1), str(ws2)}
    candidates = store.search_candidates([str(ws1), str(ws2)])
    assert {c["workspace_root"] for c in candidates} == {str(ws1), str(ws2)}


async def test_tool_with_no_granted_roots_says_no_workspace_granted(
        indexer, tmp_path):
    ws1, _ws2 = await _indexed_two_workspaces(indexer, tmp_path)
    tool = create_workspace_search_tool()
    with patch("aether_engine.tools.workspace_search.get_prose_indexer",
               return_value=indexer):
        for no_roots in (None, []):
            text = await tool.execute_fn(
                query="zebra protocol", workspace_roots=no_roots)
            assert "No workspace" in text
            assert "zebra protocol is documented" not in text
            assert str(ws1) not in text
