"""P4d steps 3–4 (orchestration programme, slice K-13): owner
collections management and the reranker.

Step 3: the collections registry + add-folder flow at the engine
level (``aether_engine.knowledge.collections``) — config surface,
indexing jobs, stats — over the step 1–2 ingestion job.

Step 4: the reranker wired into ``KnowledgeBase.search``, and the
plan's eval deliverable in its step-4 form: the SAME 20-pair eval
set as step 1 (imported from test_p4d_knowledge_base, not a copy),
re-run with the reranker off ("before") and on ("after").

EVAL NUMBERS (recorded 19 Nov 2026, this suite's own runs):
  before (fusion only):        hit@3 = 1.00, MRR@3 = 0.942
  after (reranker, rank form): hit@3 = 0.95, MRR@3 = 0.867
  after (magnitude blend, an earlier form, rejected):
                               hit@3 = 0.95, MRR@3 = 0.783
Honest reading: on this harness the reranker HURTS. The embedder
here is the deterministic bag-of-words fake (no live Ollama in
this environment); its cosine ranks are noise for keyword-precise
questions ("What does the rollback verify…"), while the fusion it
would overrule is carried by BM25. That measures the harness, not
the real qwen3 embedder — but it is the only measurement this
environment offers, so the stage ships OPT-IN
(``UserConfig.knowledge_rerank``, default off): the shipped
default keeps the step-1 bar (asserted below), the wiring is
live for the day a measured real embedder — or P3b's tier
serving a cross-encoder — justifies flipping it, and the
reranked run is still held to a floor (expected source in the
top 5 for every pair) so the stage cannot silently bury results.

The fake embedder patches the shared ``litellm.aembedding`` seam
that ``episodic.embed_text`` — the one embedder path — calls,
exactly as test_p4d_knowledge_base does.
"""

import math
import re
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_p4d_knowledge_base import EVAL_PAIRS, _write_corpus

from aether_engine.config import UserConfig
from aether_engine.knowledge import (
    CollectionExistsError,
    KnowledgeBase,
    UnknownCollectionError,
    add_folder,
    create_collection,
    get_collection,
    index_collection,
    list_collections,
    load_manifest,
    reindex_collection,
    remove_collection,
    remove_folder,
)
from aether_engine.knowledge.rerank import rerank_scored_nodes
from aether_engine.memory import episodic
from aether_engine.tools.knowledge_search import (
    clear_knowledge_base_cache,
    search_knowledge_base,
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
def _fake_embedder():
    # Reset BOTH halves of the shared embedder's failure state —
    # see the note in test_p4d_knowledge_base's fixture: leaving
    # _embed_endpoint_suspect set makes embed_text probe the dead
    # endpoint and fail before the patched aembedding runs.
    episodic._embed_unavailable_until = 0.0
    episodic._embed_endpoint_suspect = False
    clear_knowledge_base_cache()
    with patch("aether_engine.memory.episodic.litellm.aembedding",
               new=AsyncMock(side_effect=_fake_aembedding)):
        yield
    episodic._embed_unavailable_until = 0.0
    episodic._embed_endpoint_suspect = False
    clear_knowledge_base_cache()


@pytest.fixture
def config():
    return UserConfig()


@pytest.fixture
def knowledge_dir(tmp_path):
    return tmp_path / "knowledge"


def _folder(base: Path, name: str, files: dict[str, str]) -> Path:
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (folder / filename).write_text(text, encoding="utf-8")
    return folder


NOTES = {
    "compost.md": "# Compost\n\nCompost adds nitrogen to the soil "
                  "and feeds the worms in the garden beds.",
    "pruning.md": "# Pruning\n\nPrune the apple tree in late winter "
                  "before the buds open, cutting dead wood first.",
}
RECIPES = {
    "cake.md": "# Cake\n\nWhisk the eggs with sugar until pale, fold "
               "in the flour, and bake the cake for thirty minutes.",
}


# ---------------------------------------------------------------------------
# Step 3 — collections: config + add-folder flow + stats
# ---------------------------------------------------------------------------


class TestCollections:
    async def test_create_registers_indexes_and_reports_stats(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        stats = await create_collection(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        assert stats.doc_count == 2
        assert (knowledge_dir / "collections.json").is_file()

        infos = list_collections(knowledge_dir)
        assert [i.name for i in infos] == ["garden"]
        info = infos[0]
        assert info.folders == [str(notes.resolve())]
        assert info.indexed is True
        assert info.doc_count == 2
        assert info.chunk_count >= 2
        assert info.embedder == "qwen3-embedding:0.6b"
        assert info.indexed_at == stats.indexed_at

    async def test_create_duplicate_name_refused(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        await create_collection(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        with pytest.raises(CollectionExistsError):
            await create_collection(
                "garden", notes, knowledge_dir=knowledge_dir, config=config)

    @pytest.mark.parametrize("bad", ["", "  ", "a/b", "..", ".hidden",
                                     "my.notes", "a\\b"])
    async def test_invalid_names_refused(self, knowledge_dir, config, bad):
        with pytest.raises(ValueError):
            await create_collection(
                bad, Path(__file__).parent,
                knowledge_dir=knowledge_dir, config=config)
        assert list_collections(knowledge_dir) == []

    async def test_create_with_missing_folder_registers_nothing(
            self, tmp_path, knowledge_dir, config):
        with pytest.raises(FileNotFoundError):
            await create_collection(
                "garden", tmp_path / "nope",
                knowledge_dir=knowledge_dir, config=config)
        assert list_collections(knowledge_dir) == []
        with pytest.raises(UnknownCollectionError):
            get_collection("garden", knowledge_dir)

    async def test_add_folder_flow_with_prefixed_citations(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        recipes = _folder(tmp_path / "src", "recipes", RECIPES)
        await create_collection(
            "home", notes, knowledge_dir=knowledge_dir, config=config)
        stats = await add_folder(
            "home", recipes, knowledge_dir=knowledge_dir, config=config)
        assert stats.doc_count == 3
        by_path = {o.path: o.status for o in stats.outcomes}
        # The documented 1→N transition (knowledge.ingest): gaining
        # a second folder prefixes every identity, so the first
        # folder's files are re-indexed under their new identities
        # and the bare ones removed, this once.
        assert by_path["notes/compost.md"] == "indexed"
        assert by_path["compost.md"] == "removed"
        assert by_path["recipes/cake.md"] == "indexed"
        # After the transition, runs are incremental again.
        again = await reindex_collection(
            "home", knowledge_dir=knowledge_dir, config=config)
        assert {o.path: o.status for o in again.outcomes} == {
            "notes/compost.md": "unchanged",
            "notes/pruning.md": "unchanged",
            "recipes/cake.md": "unchanged",
        }

        base = KnowledgeBase(knowledge_dir / "home", config=config)
        outcome = await base.search("how long to bake the cake", top_k=3)
        assert outcome.hits[0].source_path == "recipes/cake.md"

        info = get_collection("home", knowledge_dir)
        assert info.folders == [str(notes.resolve()), str(recipes.resolve())]
        assert info.doc_count == 3

    async def test_add_folder_twice_registers_once(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        await create_collection(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        stats = await add_folder(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        assert stats.doc_count == 2
        assert get_collection("garden", knowledge_dir).folders == [
            str(notes.resolve())]

    async def test_add_folder_unknown_collection(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        with pytest.raises(UnknownCollectionError):
            await add_folder(
                "nope", notes, knowledge_dir=knowledge_dir, config=config)

    async def test_same_named_folders_get_unique_prefixes(
            self, tmp_path, knowledge_dir, config):
        docs_a = _folder(tmp_path / "a", "docs",
                         {"x.md": "# X\n\nXenon lamps glow bright violet."})
        docs_b = _folder(tmp_path / "b", "docs",
                         {"y.md": "# Y\n\nYttrium is a rare earth metal."})
        stats = await create_collection(
            "pair", [docs_a, docs_b],
            knowledge_dir=knowledge_dir, config=config)
        assert stats.doc_count == 2
        assert {o.path for o in stats.outcomes} == {"docs/x.md", "docs~2/y.md"}
        base = KnowledgeBase(knowledge_dir / "pair", config=config)
        outcome = await base.search("rare earth metal yttrium", top_k=2)
        assert outcome.hits[0].source_path == "docs~2/y.md"

    async def test_remove_folder_drops_its_documents(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        recipes = _folder(tmp_path / "src", "recipes", RECIPES)
        await create_collection(
            "home", [notes, recipes],
            knowledge_dir=knowledge_dir, config=config)
        stats = await remove_folder(
            "home", recipes, knowledge_dir=knowledge_dir, config=config)
        assert stats.doc_count == 2
        assert {o.path: o.status for o in stats.outcomes}[
            "recipes/cake.md"] == "removed"
        with pytest.raises(ValueError):
            await remove_folder(
                "home", notes, knowledge_dir=knowledge_dir, config=config)

    async def test_reindex_picks_up_edits(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        await create_collection(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        (notes / "compost.md").write_text(
            "# Compost\n\nCompost now mentions biochar and leaf mould.",
            encoding="utf-8")
        stats = await reindex_collection(
            "garden", knowledge_dir=knowledge_dir, config=config)
        assert {o.path: o.status for o in stats.outcomes}[
            "compost.md"] == "indexed"
        base = KnowledgeBase(knowledge_dir / "garden", config=config)
        outcome = await base.search("biochar leaf mould", top_k=2)
        assert "biochar" in outcome.hits[0].text

    async def test_remove_collection_unregisters_but_keeps_index(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        await create_collection(
            "garden", notes, knowledge_dir=knowledge_dir, config=config)
        remove_collection("garden", knowledge_dir)
        assert list_collections(knowledge_dir) == []
        with pytest.raises(UnknownCollectionError):
            get_collection("garden", knowledge_dir)
        # The persisted index is the owner's data: left on disk.
        assert load_manifest(knowledge_dir / "garden") is not None
        with pytest.raises(UnknownCollectionError):
            remove_collection("garden", knowledge_dir)

    async def test_tool_searches_a_managed_collection(
            self, tmp_path, knowledge_dir, config):
        notes = _folder(tmp_path / "src", "notes", NOTES)
        recipes = _folder(tmp_path / "src", "recipes", RECIPES)
        await create_collection(
            "home", [notes, recipes],
            knowledge_dir=knowledge_dir, config=config)
        text = await search_knowledge_base(
            "what does compost add to the soil?", "home",
            knowledge_dir=knowledge_dir, config=config)
        assert "notes/compost.md" in text
        assert "nitrogen" in text


# ---------------------------------------------------------------------------
# Step 4 — the reranker
# ---------------------------------------------------------------------------


def _scored(embedding, score):
    return SimpleNamespace(
        node=SimpleNamespace(embedding=embedding), score=score)


class TestRerankerUnit:
    def test_cosine_rank_can_overturn_fusion_order(self):
        query = [1.0, 0.0]
        # Fusion order A > B > C; cosine order B > C > A. Rank
        # fusion (1/(60+fusion_rank) + 1/(60+cosine_rank)) puts B
        # first: B fuses ranks (2,1), A fuses (1,3), C fuses (3,2).
        a = _scored([0.0, 1.0], 1.0)   # fusion 1st, cosine last
        b = _scored([1.0, 0.0], 0.5)   # fusion 2nd, cosine 1st
        c = _scored([0.6, 0.8], 0.25)  # fusion 3rd, cosine 2nd
        reranked, applied = rerank_scored_nodes(query, [a, b, c], top_n=3)
        assert applied is True
        assert [r.node for r in reranked] == [b.node, a.node, c.node]
        assert reranked[0].retrieval_score == 0.5
        assert reranked[1].retrieval_score == 1.0

    def test_no_embeddings_means_not_applied(self):
        reranked, applied = rerank_scored_nodes(
            [1.0, 0.0], [_scored(None, 1.0), _scored(None, 0.5)], top_n=2)
        assert applied is False
        assert reranked == []

    def test_top_n_cut(self):
        query = [1.0, 0.0]
        nodes = [_scored([1.0, 0.0], 0.1 * i) for i in range(5)]
        reranked, applied = rerank_scored_nodes(query, nodes, top_n=2)
        assert applied is True
        assert len(reranked) == 2


class TestRerankerInSearch:
    @pytest.fixture
    async def indexed(self, tmp_path, config):
        corpus = _write_corpus(tmp_path / "corpus")
        collection_dir = tmp_path / "knowledge" / "default"
        await index_collection(corpus, collection_dir, config=config)
        return collection_dir

    async def test_search_reports_rerank_and_keeps_fusion_scores(
            self, indexed, config):
        base = KnowledgeBase(indexed, config=config, rerank=True)
        outcome = await base.search("What does compost add to the soil?", top_k=3)
        assert outcome.reranked is True
        assert outcome.reranker == "embedding-cosine"
        assert "gardening.md" in [h.source_path for h in outcome.hits]
        assert all(h.retrieval_score is not None for h in outcome.hits)

    async def test_rerank_can_be_switched_off(self, indexed, config):
        base = KnowledgeBase(indexed, config=config, rerank=False)
        outcome = await base.search("What does compost add to the soil?", top_k=3)
        assert outcome.reranked is False
        assert outcome.reranker == ""
        assert all(h.retrieval_score is None for h in outcome.hits)
        assert outcome.hits[0].source_path == "gardening.md"

    async def test_embedder_down_skips_reranker(self, indexed, config):
        episodic._embed_unavailable_until = time.monotonic() + 60.0
        base = KnowledgeBase(indexed, config=config, rerank=True)
        outcome = await base.search("compost nitrogen soil", top_k=3)
        assert outcome.degraded is True
        assert outcome.reranked is False
        assert outcome.hits  # BM25 still answers

    async def test_tool_text_notes_the_rerank(self, indexed):
        # The config switch is the owner's on-ramp for the stage.
        rerank_config = UserConfig(knowledge_rerank=True)
        text = await search_knowledge_base(
            "what does compost add to the soil?",
            knowledge_dir=indexed.parent, config=rerank_config)
        assert "reranked locally by embedding-cosine" in text


# ---------------------------------------------------------------------------
# Step 4 — the eval deliverable: step 1's set, before vs after
# ---------------------------------------------------------------------------


class TestEvalStep4:
    async def test_eval_before_after(self, tmp_path, config):
        corpus = _write_corpus(tmp_path / "corpus")
        collection_dir = tmp_path / "knowledge" / "default"
        await index_collection(corpus, collection_dir, config=config)

        async def measure(rerank: bool | None) -> tuple[float, float, float]:
            base = KnowledgeBase(collection_dir, config=config, rerank=rerank)
            hits_at_3 = 0
            hits_at_5 = 0
            reciprocal_rank = 0.0
            for question, expected in EVAL_PAIRS:
                outcome = await base.search(question, top_k=5)
                sources = [h.source_path for h in outcome.hits]
                if expected in sources[:3]:
                    hits_at_3 += 1
                    reciprocal_rank += 1.0 / (sources.index(expected) + 1)
                if expected in sources:
                    hits_at_5 += 1
            n = len(EVAL_PAIRS)
            return hits_at_3 / n, reciprocal_rank / n, hits_at_5 / n

        before = await measure(False)
        after = await measure(True)
        shipped = await measure(None)  # the config default: off
        print(
            f"\nP4d step-4 eval (deterministic fake embedder): "
            f"before hit@3={before[0]:.2f} MRR@3={before[1]:.3f}; "
            f"after hit@3={after[0]:.2f} MRR@3={after[1]:.3f} "
            f"hit@5={after[2]:.2f}")
        # The shipped default keeps the step-1 bar exactly: every
        # expected source in the top 3, as in K-07's eval test.
        assert shipped[0] == 1.0
        assert before[0] == 1.0
        # The opt-in reranker is held to its floor: it may reorder,
        # but on this harness it must not bury any expected source
        # out of the top 5. (Its hit@3/MRR regression on the fake
        # embedder is recorded in this module's docstring — the
        # reason the stage is opt-in, not silently on.)
        assert after[2] == 1.0, f"reranked eval buried a source: {after}"
