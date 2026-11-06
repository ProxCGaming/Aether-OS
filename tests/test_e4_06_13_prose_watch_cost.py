"""E4-06 / E4-13: the prose watcher and status must not re-read the tree.

E4-06: the watch loop runs a full ``scan_workspace`` every 2.0 s per
root, and the scan opens and reads the 8 KB head of EVERY prose
candidate — changed or not — for its binary test. The unchanged-file
fast paths live in ``_index_file``, *after* the scan has already paid
the walk + head reads, so an idle, fully indexed workspace costs a
complete walk and per-file reads every two seconds for the process's
whole life. Fix: the scan skips the head read for a candidate the
store already knows as indexable with the same (mtime_ns, size) —
the module's own unchanged-file identity (``_index_file`` fast-paths
on exactly that pair) — so an idle pass is walk + stat only. Changed
files are still read, classified and re-indexed exactly as before.

E4-13: ``status()`` is a read-only report, but ``_excluded_for``
falls through to a full synchronous ``scan_workspace`` whenever a
root has no excluded documents in the store and no cached scan —
and because an exclusion-free scan leaves the by-path map empty,
*every* status call for such a workspace re-scans. Fix: status
answers from store state + the last index pass's exclusions only;
discovery belongs to index passes and the watcher, never to a
status poll.

The embedder fakes mirror test_prose_indexer.py (deterministic
bag-of-words over ``litellm.aembedding``; no live Ollama exists in
this environment).
"""
import math
import re
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.memory import episodic
from aether_engine.rag import prose_indexer
from aether_engine.rag.prose_indexer import ProseIndexer
from aether_engine.rag.store import WorkspaceRagStore


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
    episodic._embed_endpoint_suspect = False
    yield
    episodic._embed_unavailable_until = 0.0
    episodic._embed_endpoint_suspect = False


@pytest.fixture
def indexer(tmp_path):
    return ProseIndexer(store=WorkspaceRagStore(tmp_path / "workspace_rag.db"))


def _patch_embedder():
    return patch("aether_engine.memory.episodic.litellm.aembedding",
                 new=AsyncMock(side_effect=_fake_aembedding))


# ---------------------------------------------------------------------------
# E4-13: status() performs no scan
# ---------------------------------------------------------------------------

def test_status_performs_no_scan(indexer, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "guide.md").write_text("# Guide\n\nNever indexed.\n", encoding="utf-8")

    scans = []
    real_scan = prose_indexer.scan_workspace

    def counting_scan(*args, **kwargs):
        scans.append(args)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(prose_indexer, "scan_workspace", counting_scan)

    # A never-indexed, exclusion-free root is exactly the case that
    # used to fall through to a full scan — on every single poll.
    first = indexer.status(str(ws))
    second = indexer.status(str(ws))

    assert scans == []
    assert first["excluded_files"] == []
    assert second["excluded_files"] == []
    assert first["total_files"] == 0


# ---------------------------------------------------------------------------
# E4-06: an unchanged-file pass performs no head reads; a changed file
# is still picked up
# ---------------------------------------------------------------------------

async def test_watch_pass_reads_nothing_for_unchanged_files(
        indexer, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    guide = ws / "guide.md"
    guide.write_text("# Guide\n\nConfiguration parameters live here.\n",
                     encoding="utf-8")
    indexer.grant_consent(str(ws))
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    assert indexer.status(str(ws))["indexed_files"] == 1

    opened: list[str] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        if self == guide:
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    # Second pass over an unchanged tree (what the watcher does every
    # cycle): the scan must not open the file's head again.
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    assert opened == []

    # Control: a real change (different size, so the (mtime, size)
    # identity cannot collide) is still discovered and re-indexed.
    guide.write_text(
        "# Guide\n\nConfiguration parameters live here. Brand new text "
        "about watchers and scans and incremental indexing passes.\n",
        encoding="utf-8")
    with _patch_embedder():
        await indexer.index_workspace(str(ws))
    docs = indexer.store.list_documents(str(ws))
    chunks = indexer.store.chunks_for_document(docs[0]["id"])
    assert any("Brand new text" in c["content"] for c in chunks)
