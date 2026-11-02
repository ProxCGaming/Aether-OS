"""Epic 8, E8-3.2: prose indexer + unified embedder + consent gate.

The index is PROSE ONLY (grill Q16): code is never embedded — the
repo map (E8-3.1) covers code. Embeddings are local-first (Q17) via
ONE embedder shared with episodic memory: ``qwen3-embedding:0.6b``
through Ollama. First index per workspace waits for the user's go
(consent), with file count / size / destination shown first; the
embedding endpoint being down queues work instead of losing it.

No live Ollama exists in this environment: every embedding here is a
deterministic bag-of-words fake patched over ``litellm.aembedding``
(the seam the episodic tests already use), and the offline cases use
a fake that raises ``ConnectionError`` so the shared Epic 9 circuit
breaker trips for real.
"""
import itertools
import math
import os
import re
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.memory import episodic
from aether_engine.rag.prose_indexer import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MAX_FILE_BYTES,
    ConsentRequired,
    ProseIndexer,
    chunk_text,
    scan_workspace,
)
from aether_engine.rag.store import WorkspaceRagStore

# ----------------------------------------------------------------------
# Fakes / fixtures
# ----------------------------------------------------------------------

def _vec(text: str, dims: int = 64) -> list[float]:
    """Deterministic bag-of-words embedding: texts sharing words get
    similar vectors, so semantic ranking is exercisable offline."""
    v = [0.0] * dims
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        v[zlib.crc32(tok.encode()) % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _fake_aembedding(model=None, input=None, **_kwargs):
    resp = type("Resp", (), {})()
    resp.data = [{"embedding": _vec(t)} for t in (input or [])]
    return resp


def _patch_embedder(*, fail: bool = False):
    if fail:
        mock = AsyncMock(side_effect=ConnectionError("Ollama down"))
    else:
        mock = AsyncMock(side_effect=_fake_aembedding)
    return patch("aether_engine.memory.episodic.litellm.aembedding", new=mock)


@pytest.fixture(autouse=True)
def _reset_breaker():
    """The embedding circuit breaker is process state; every test here
    starts and ends with it closed (a tripped breaker is simulated
    explicitly inside the offline test)."""
    episodic._embed_unavailable_until = 0.0
    yield
    episodic._embed_unavailable_until = 0.0


@pytest.fixture
def store(tmp_path):
    return WorkspaceRagStore(tmp_path / "workspace_rag.db")


@pytest.fixture
def indexer(store):
    return ProseIndexer(store=store)


def _write(root: Path, rel: str, content, *, binary: bool = False) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def _mixed_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    _write(ws, "guide.md", "# Guide\n\nConfiguration parameters live here.\n")
    _write(ws, "notes.txt", "Plain text notes about indexing.\n")
    _write(ws, "app.py", "print('code is never embedded')\n")
    _write(ws, "web.ts", "export const x: number = 1;\n")
    _write(ws, "docs/deep.markdown", "Deep prose in a subfolder.\n")
    return ws


# ----------------------------------------------------------------------
# WS contracts (PRD §E8-3.2)
# ----------------------------------------------------------------------

def test_index_event_types_exist():
    assert EventType.WORKSPACE_INDEX_START_REQUEST.value == "WORKSPACE_INDEX_START_REQUEST"
    assert EventType.WORKSPACE_INDEX_START_RESPONSE.value == "WORKSPACE_INDEX_START_RESPONSE"
    assert EventType.WORKSPACE_INDEX_STATUS_REQUEST.value == "WORKSPACE_INDEX_STATUS_REQUEST"
    assert EventType.WORKSPACE_INDEX_STATUS_RESPONSE.value == "WORKSPACE_INDEX_STATUS_RESPONSE"
    assert EventType.WORKSPACE_INDEX_PROGRESS.value == "WORKSPACE_INDEX_PROGRESS"


# ----------------------------------------------------------------------
# Chunking (1500 chars / 200 overlap, line spans resolve to real text)
# ----------------------------------------------------------------------

def test_chunking_respects_size_and_line_spans():
    lines = [f"line {i} " + "x" * 90 for i in range(60)]
    text = "\n".join(lines)
    chunks = chunk_text(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= CHUNK_SIZE
        # The span must resolve to exactly the chunk's text.
        assert c.content == "\n".join(lines[c.line_start - 1:c.line_end])
        assert c.line_start >= 1 and c.line_end >= c.line_start


def test_chunking_overlaps_neighbours():
    lines = [f"unique line number {i} " + "y" * 80 for i in range(80)]
    text = "\n".join(lines)
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    for prev, nxt in itertools.pairwise(chunks):
        # Overlap: the next chunk starts at or before the previous
        # chunk's end line, they share at least one full line, and
        # the shared region stays within the overlap budget.
        assert nxt.line_start <= prev.line_end
        shared = list(range(nxt.line_start, prev.line_end + 1))
        assert shared, "consecutive chunks must share lines (overlap)"
        shared_text = "\n".join(lines[s - 1] for s in shared)
        assert len(shared_text) <= CHUNK_OVERLAP


def test_chunking_single_long_line_is_hard_split():
    text = "z" * (CHUNK_SIZE * 3)
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.content) <= CHUNK_SIZE
        assert c.line_start == 1 and c.line_end == 1
    # Hard-split pieces overlap; the first piece is a strict prefix
    # of the source line.
    assert text.startswith(chunks[0].content)


def test_chunking_empty_text_gives_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunking_short_text_is_one_chunk():
    chunks = chunk_text("hello\nworld")
    assert len(chunks) == 1
    assert chunks[0].line_start == 1 and chunks[0].line_end == 2
    assert chunks[0].content == "hello\nworld"


# ----------------------------------------------------------------------
# Scan: prose only + exclusions with reasons
# ----------------------------------------------------------------------

def test_scan_finds_only_prose(tmp_path):
    ws = _mixed_workspace(tmp_path)
    result = scan_workspace(str(ws))
    rels = {e.rel_path for e in result.indexable}
    assert rels == {"guide.md", "notes.txt", "docs/deep.markdown"}


def test_scan_exclusions_carry_reasons(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "keep.md", "indexed prose\n")
    _write(ws, ".gitignore", "secret.md\n")
    _write(ws, "secret.md", "gitignored prose\n")
    _write(ws, "binary.md", b"\x00\x01\x02binary", binary=True)
    _write(ws, "huge.md", "a" * (MAX_FILE_BYTES + 10))
    _write(ws, "yarn.lock", "# lockfile contents\n")
    result = scan_workspace(str(ws))
    reasons = {e.rel_path: e.reason for e in result.excluded}
    assert reasons["secret.md"] == "gitignore"
    assert reasons["binary.md"] == "binary"
    assert reasons["huge.md"] == "too_large"
    assert reasons["yarn.lock"] == "lockfile"
    assert {e.rel_path for e in result.indexable} == {"keep.md"}


def test_scan_prunes_ignored_directories(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "node_modules/pkg/README.md", "dependency docs\n")
    _write(ws, ".git/objects/note.md", "vcs internals\n")
    _write(ws, "real.md", "real prose\n")
    result = scan_workspace(str(ws))
    assert {e.rel_path for e in result.indexable} == {"real.md"}


# ----------------------------------------------------------------------
# Scan: symlink escapes (F-04) — a link inside the workspace pointing
# outside the granted root must never be chunked / embedded, while a
# link pointing INSIDE the root still indexes normally.
# ----------------------------------------------------------------------

CANARY = "CANARY-SYMLINK-SECRET-9f3b2a"


def _all_chunk_contents(indexer, root: str) -> str:
    return "\n".join(
        c["content"]
        for d in indexer.store.list_documents(root)
        for c in indexer.store.chunks_for_document(d["id"])
    )


async def test_symlinked_file_pointing_outside_is_not_indexed(indexer, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = _write(tmp_path, "outside/secret.md", f"outside {CANARY}\n")
    _write(ws, "real.md", "legitimate workspace prose\n")
    os.symlink(outside, ws / "leaked.md")

    result = scan_workspace(str(ws))
    assert "leaked.md" not in {e.rel_path for e in result.indexable}
    reasons = {e.rel_path: e.reason for e in result.excluded}
    assert reasons.get("leaked.md") == "symlink_escape"
    assert "real.md" in {e.rel_path for e in result.indexable}

    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    contents = _all_chunk_contents(indexer, str(ws))
    assert CANARY not in contents
    assert "legitimate workspace prose" in contents
    status = indexer.status(str(ws))
    excluded = {e["file_path"]: e["reason"] for e in status["excluded_files"]}
    assert excluded.get("leaked.md") == "symlink_escape"


async def test_symlinked_dir_pointing_outside_is_not_indexed(indexer, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside_file = _write(
        tmp_path, "outside_docs/hidden.md", f"dir escape {CANARY}\n")
    outside_dir = outside_file.parent
    _write(ws, "real.md", "legitimate workspace prose\n")
    os.symlink(outside_dir, ws / "linked_docs", target_is_directory=True)

    result = scan_workspace(str(ws))
    rels = {e.rel_path for e in result.indexable}
    assert not any(r.startswith("linked_docs") for r in rels)
    reasons = {e.rel_path: e.reason for e in result.excluded}
    assert reasons.get("linked_docs") == "symlink_escape"

    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    contents = _all_chunk_contents(indexer, str(ws))
    assert CANARY not in contents
    # Nothing under the linked dir is indexed/pending — at most the
    # directory itself is recorded, status-only, as excluded.
    docs = indexer.store.list_documents(str(ws))
    assert not any(
        d["file_path"].startswith("linked_docs/")
        for d in docs)
    assert all(
        d["status"] == "excluded"
        for d in docs if d["file_path"] == "linked_docs")


async def test_symlink_to_file_inside_workspace_still_indexed(indexer, tmp_path):
    ws = tmp_path / "ws"
    target = _write(ws, "docs/real.md", "in-workspace linked prose\n")
    os.symlink(target, ws / "alias.md")

    result = scan_workspace(str(ws))
    rels = {e.rel_path for e in result.indexable}
    assert "alias.md" in rels
    assert "docs/real.md" in rels
    assert "alias.md" not in {e.rel_path for e in result.excluded}

    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    contents = _all_chunk_contents(indexer, str(ws))
    assert "in-workspace linked prose" in contents
    docs = {d["file_path"]: d for d in indexer.store.list_documents(str(ws))}
    assert docs["alias.md"]["status"] == "indexed"


# ----------------------------------------------------------------------
# Consent gate + estimate (first index waits for the user's go)
# ----------------------------------------------------------------------

async def test_index_requires_consent(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    est = indexer.estimate(str(ws))
    assert est["estimated_files"] == 3
    assert est["estimated_bytes"] > 0
    with pytest.raises(ConsentRequired):
        await indexer.index_workspace(str(ws))


async def test_consent_persists_in_store(store, tmp_path):
    ws = _mixed_workspace(tmp_path)
    first = ProseIndexer(store=store)
    assert not first.has_consent(str(ws))
    first.grant_consent(str(ws))
    second = ProseIndexer(store=store)
    assert second.has_consent(str(ws))


# ----------------------------------------------------------------------
# Indexing end to end (fake embedder)
# ----------------------------------------------------------------------

async def test_index_records_only_prose_documents(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    with _patch_embedder():
        report = await indexer.index_workspace(str(ws))
    docs = indexer.store.list_documents(str(ws))
    paths = {d["file_path"] for d in docs}
    assert "app.py" not in paths and "web.ts" not in paths
    assert {"guide.md", "notes.txt", "docs/deep.markdown"} <= paths
    assert report["indexed_files"] == 3
    status = indexer.status(str(ws))
    assert status["indexed_files"] == 3
    assert status["total_chunks"] > 0
    assert status["embedder"] == "qwen3-embedding:0.6b"
    assert status["status"] == "idle"
    assert status["paused"] is False


async def test_index_progress_callback_fires(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    seen = []
    with _patch_embedder():
        await indexer.index_workspace(
            str(ws), progress_cb=lambda **kw: seen.append(kw))
    assert seen, "progress callback must fire during indexing"
    assert seen[-1]["indexed"] == seen[-1]["total"] == 3
    assert all(s["workspace_root"] == str(ws) for s in seen)


async def test_offline_indexing_queues_and_backfills(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))

    # Endpoint down: documents and chunks are recorded, embeddings
    # stay pending — nothing is lost.
    with _patch_embedder(fail=True):
        report = await indexer.index_workspace(str(ws))
    assert report["indexed_files"] == 0
    pending = indexer.store.pending_chunks()
    assert pending, "chunks must be queued while the endpoint is down"
    docs = {d["file_path"]: d for d in indexer.store.list_documents(str(ws))}
    assert docs["guide.md"]["status"] == "pending"

    # While the breaker is still open, a backfill attempt embeds
    # nothing (it must not hammer the dead endpoint) and loses nothing.
    result = await indexer.backfill_pending()
    assert result["embedded"] == 0
    assert indexer.store.pending_chunks()

    # Cooldown over + endpoint back: the queue drains completely.
    episodic._embed_unavailable_until = 0.0
    with _patch_embedder():
        result = await indexer.backfill_pending()
    assert result["embedded"] > 0
    assert indexer.store.pending_chunks() == []
    status = indexer.status(str(ws))
    assert status["indexed_files"] == 3


async def test_incremental_reindex_touches_only_changed_files(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    before = {
        c["id"]: c["content"]
        for d in indexer.store.list_documents(str(ws))
        for c in indexer.store.chunks_for_document(d["id"])
    }
    guide_doc = next(
        d for d in indexer.store.list_documents(str(ws))
        if d["file_path"] == "guide.md")
    guide_chunks_before = {
        c["id"] for c in indexer.store.chunks_for_document(guide_doc["id"])}

    _write(ws, "guide.md", "# Guide\n\nTotally new configuration text.\n")
    with _patch_embedder():
        await indexer.index_workspace(str(ws))

    after_docs = indexer.store.list_documents(str(ws))
    for d in after_docs:
        chunks = indexer.store.chunks_for_document(d["id"])
        if d["file_path"] == "guide.md":
            contents = " ".join(c["content"] for c in chunks)
            assert "Totally new configuration text" in contents
        else:
            # Untouched files keep their exact chunk rows.
            assert {c["id"] for c in chunks} <= set(before)
    assert guide_chunks_before  # sanity: the doc had chunks before


async def test_deleted_file_is_removed_from_index(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    (ws / "notes.txt").unlink()
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    paths = {d["file_path"] for d in indexer.store.list_documents(str(ws))}
    assert "notes.txt" not in paths
    assert "guide.md" in paths


# ----------------------------------------------------------------------
# Embedder switch = full re-index (vectors are incomparable)
# ----------------------------------------------------------------------

async def test_embedder_switch_drops_chunks_until_reindex(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    assert indexer.status(str(ws))["total_chunks"] > 0

    indexer.set_embedder("qwen3-embedding:4b")
    status = indexer.status(str(ws))
    assert status["embedder"] == "qwen3-embedding:4b"
    assert status["total_chunks"] == 0
    assert status["indexed_files"] == 0

    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    status = indexer.status(str(ws))
    assert status["total_chunks"] > 0
    assert status["indexed_files"] == 3


# ----------------------------------------------------------------------
# Watcher bookkeeping (the loop itself is a thin poll over
# index_workspace + backfill_pending, both covered above)
# ----------------------------------------------------------------------

async def test_watcher_start_stop(indexer, tmp_path):
    ws = _mixed_workspace(tmp_path)
    indexer.grant_consent(str(ws))
    assert indexer.watching_roots == []
    indexer.start_watching(str(ws))
    assert indexer.watching_roots == [str(ws)]
    indexer.start_watching(str(ws))  # idempotent
    assert indexer.watching_roots == [str(ws)]
    await indexer.stop_watching(str(ws))
    assert indexer.watching_roots == []


# ----------------------------------------------------------------------
# Unified embedder resolution (ONE embedder: episodic + prose index)
# ----------------------------------------------------------------------

def test_unified_embedder_defaults():
    assert episodic.DEFAULT_EMBEDDER == "qwen3-embedding:0.6b"
    assert episodic.resolve_embedder() == "qwen3-embedding:0.6b"
    assert episodic.EMBEDDING_MODEL == "ollama/qwen3-embedding:0.6b"
    assert episodic.litellm_embedding_model() == "ollama/qwen3-embedding:0.6b"


def test_unified_embedder_honours_config(tmp_path):
    cfg = UserConfig(embedding_model="qwen3-embedding:4b")
    assert episodic.resolve_embedder(cfg) == "qwen3-embedding:4b"
    assert episodic.litellm_embedding_model(cfg) == "ollama/qwen3-embedding:4b"
    # The prose index resolves through the same path.
    idx = ProseIndexer(
        store=WorkspaceRagStore(tmp_path / "rag.db"), config=cfg)
    assert idx.embedder == "qwen3-embedding:4b"


def test_userconfig_embedding_model_round_trips():
    cfg = UserConfig()
    assert cfg.embedding_model == "qwen3-embedding:0.6b"
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.embedding_model == "qwen3-embedding:0.6b"
    custom = UserConfig.from_dict({"embedding_model": "qwen3-embedding:4b"})
    assert custom.embedding_model == "qwen3-embedding:4b"


async def test_episodic_store_uses_unified_embedder(tmp_path):
    """Episodic memory embeds through the same resolved model: the
    fake endpoint must be called with the unified LiteLLM name."""
    seen_models = []

    async def capturing(model=None, input=None, **_kwargs):
        seen_models.append(model)
        return await _fake_aembedding(model=model, input=input)

    test_db = tmp_path / "epi.db"
    test_emb_db = tmp_path / "emb.db"
    real_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or db_path == episodic.DB_PATH:
            return real_get_conn(test_db)
        if db_path == episodic.EMBEDDINGS_DB_PATH:
            return real_get_conn(test_emb_db)
        return real_get_conn(db_path)

    with (
        patch.object(episodic, "_get_conn", side_effect=fake_get_conn),
        patch.object(episodic._audit_logger, "log_event"),
        patch("aether_engine.memory.episodic.litellm.aembedding",
              new=AsyncMock(side_effect=capturing)),
    ):
        await episodic.store_episode(
            "task-unified-1", "prompt", "summary text", ["t"], "done")
    assert seen_models == ["ollama/qwen3-embedding:0.6b"]
