"""P4d steps 1–2 (orchestration programme, slice K-07): the
knowledge base — ingestion + persisted hybrid index over a test
corpus, and the ``search_knowledge_base`` tool.

The eval set is part of the slice (plan step 1: "eval set green"):
20 fixed question → expected-source pairs over the fixture corpus
below; every pair's expected document must appear in the top 3
hits. Retrieval regressions fail here like any other behaviour.

Embeddings are the same deterministic fake as
test_workspace_search_tool (no live Ollama in this environment),
patched over the shared ``litellm.aembedding`` seam that
``episodic.embed_text`` — the one embedder path — calls.
"""

import math
import re
import time
import zipfile
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.knowledge import (
    EmbedderMismatchError,
    KnowledgeBase,
    index_collection,
)
from aether_engine.memory import episodic
from aether_engine.tools.ceilings import apply_tool_ceiling
from aether_engine.tools.knowledge_search import (
    available_collections,
    clear_knowledge_base_cache,
    search_knowledge_base,
)
from aether_engine.tools.registry import ToolRisk, build_tool_registry


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
    # Both halves of the shared embedder's failure state must be
    # reset: the breaker timestamp AND the endpoint-suspect flag.
    # The suspect flag makes embed_text probe 127.0.0.1:11434 and
    # fail before the patched aembedding is ever reached, so any
    # earlier test that hit a real embed failure (an episodic store
    # with Ollama down) silently zeroes every ingest in this file.
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


# ---------------------------------------------------------------------------
# Fixture corpus — nine documents, one per reader family the plan
# names (Markdown, text, DOCX, code, PDF), each on a topic with
# distinctive vocabulary the eval questions draw on.
# ---------------------------------------------------------------------------

GARDENING = """# Gardening Notes

Tomato plants need full sun and deep watering twice a week.
Basil grows well beside tomatoes and keeps some pests away.
Compost adds nitrogen and organic matter to the soil.
A thick mulch layer holds moisture in the soil during hot weeks.
"""

ASTRONOMY = """# Astronomy Notes

Jupiter is the largest planet in the solar system, and its Great
Red Spot is a storm wider than Earth. Saturn is famous for rings
made of ice and rock. A telescope with a wide aperture reveals
the Orion nebula on a clear winter night.
"""

BICYCLE = """# Bicycle Maintenance

A skipping gear usually means the derailleur cable has stretched;
turn the barrel adjuster half a turn at a time. Replace brake pads
when the grooves disappear. Keep the chain clean and apply chain
lubricant every few hundred kilometres.
"""

COOKING = """# Sourdough Log

Feed the sourdough starter with equal parts flour and water every
morning. Let the dough proof for four hours at room temperature
until it has doubled. Bake in a preheated cast iron pot so the
crust caramelises and stays crisp.
"""

FINANCE = """# Household Finance

Keep an emergency fund holding three to six months of expenses in
an easy-access account. Index funds spread risk across the whole
market for a low fee. Compound interest rewards starting early:
money earns returns, and those returns earn returns of their own.
"""

MUSIC = """Plain text notes on music practice.
A metronome keeps the tempo steady while you practise scales.
A chord progression is a sequence of chords, such as the famous
one four five six pattern heard in countless songs.
Practise each scale slowly before raising the tempo.
"""

HISTORY_DOCX_PARAGRAPHS = [
    ("The harbour lighthouse was built in 1897 to guide ships into "
     "the bay."),
    ("During the monsoon season the ferry traded silk, spices and "
     "dried fish along the coast."),
    ("Keepers lived in the lighthouse cottage and polished the lamp "
     "every evening."),
]

DEPLOY_PY = '''"""Deployment helper for the staging service."""


def canary_deployment(artifact):
    """Roll out a canary deployment to five percent of servers.

    The canary servers serve a small slice of traffic while the
    health check endpoint is watched for errors.
    """
    ...


def rollback(previous):
    """Roll back to the previous artifact.

    Before restoring, the rollback verifies the artifact checksum
    and confirms the database backup finished.
    """
    ...
'''

PDF_LINES = [
    "Kitchen safety basics for every home cook.",
    "Never pour water on a grease fire; smother the flames with a",
    "lid or throw baking soda over them.",
    "Store sharp knives in a wooden block or on a magnetic strip,",
    "never loose in a drawer.",
    "Test the smoke alarm every month and replace its battery yearly.",
]


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/><Default '
        'Extension="xml" ContentType="application/xml"/><Override '
        'PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)


def _write_pdf(path: Path, lines: list[str]) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject()
    font.update({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = writer._add_object(font)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    parts = ["BT /F1 12 Tf 50 750 Td"]
    for i, line in enumerate(lines):
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        if i:
            parts.append("0 -16 Td")
        parts.append(f"({escaped}) Tj")
    parts.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(parts).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/MediaBox")] = page.mediabox
    _ = NumberObject  # imported for parity with pypdf generic usage
    with open(path, "wb") as fh:
        writer.write(fh)


def _write_corpus(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gardening.md").write_text(GARDENING, encoding="utf-8")
    (root / "astronomy.md").write_text(ASTRONOMY, encoding="utf-8")
    (root / "bicycle.md").write_text(BICYCLE, encoding="utf-8")
    (root / "cooking.md").write_text(COOKING, encoding="utf-8")
    (root / "finance.md").write_text(FINANCE, encoding="utf-8")
    (root / "music.txt").write_text(MUSIC, encoding="utf-8")
    _write_docx(root / "history.docx", HISTORY_DOCX_PARAGRAPHS)
    (root / "deploy.py").write_text(DEPLOY_PY, encoding="utf-8")
    _write_pdf(root / "kitchen-safety.pdf", PDF_LINES)
    (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nnot indexed")
    return root


@pytest.fixture
async def indexed(tmp_path, config):
    """The full corpus indexed into a collection named 'default'."""
    corpus = _write_corpus(tmp_path / "corpus")
    knowledge_dir = tmp_path / "knowledge"
    collection_dir = knowledge_dir / "default"
    stats = await index_collection(corpus, collection_dir, config=config)
    return corpus, knowledge_dir, collection_dir, stats


# ---------------------------------------------------------------------------
# Step 1 — ingestion + index
# ---------------------------------------------------------------------------


class TestIngestion:
    async def test_all_reader_families_indexed(self, indexed):
        _corpus, _kd, _cd, stats = indexed
        assert stats.doc_count == 9  # the .png is not a candidate
        by_path = {o.path: o for o in stats.outcomes}
        for name in ("gardening.md", "astronomy.md", "bicycle.md",
                     "cooking.md", "finance.md", "music.txt",
                     "history.docx", "deploy.py", "kitchen-safety.pdf"):
            assert by_path[name].status == "indexed", name
            assert by_path[name].chunks >= 1, name
        assert "photo.png" not in by_path
        assert stats.embedder == "qwen3-embedding:0.6b"
        assert stats.chunk_count >= 9

    async def test_manifest_records_embedder_and_hashes(self, indexed, config):
        _corpus, _kd, collection_dir, _stats = indexed
        from aether_engine.knowledge import load_manifest

        manifest = load_manifest(collection_dir)
        assert manifest["embedder"] == episodic.resolve_embedder(config)
        assert manifest["doc_count"] == 9
        assert set(manifest["files"]) >= {"gardening.md", "deploy.py"}
        assert all(len(f["sha256"]) == 64 for f in manifest["files"].values())

    async def test_long_document_chunks_with_positions(self, tmp_path, config):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        paragraph = (
            "Quilting bees gathered every Thursday in the village hall. "
            "Members stitched patchwork quilts from cotton scraps, traded "
            "fabric patterns, and taught beginners the running stitch. ")
        (corpus / "long.md").write_text(paragraph * 60, encoding="utf-8")
        collection_dir = tmp_path / "knowledge" / "default"
        stats = await index_collection(corpus, collection_dir, config=config)
        assert stats.doc_count == 1
        assert stats.chunk_count >= 3
        base = KnowledgeBase(collection_dir, config=config)
        outcome = await base.search("quilting patchwork stitch", top_k=5)
        assert outcome.hits
        indices = [h.chunk_index for h in outcome.hits]
        assert all(i is not None and i >= 0 for i in indices)
        assert all(h.source_path == "long.md" for h in outcome.hits)
        assert all(h.start_char is not None and h.end_char is not None
                   for h in outcome.hits)

    async def test_incremental_reindex_by_hash(self, tmp_path, config):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "alpha.md").write_text(
            "# Alpha\n\nZebras graze on the open savanna at dawn.",
            encoding="utf-8")
        (corpus / "beta.md").write_text(
            "# Beta\n\nPenguins huddle against the Antarctic wind.",
            encoding="utf-8")
        collection_dir = tmp_path / "knowledge" / "default"
        first = await index_collection(corpus, collection_dir, config=config)
        assert first.doc_count == 2

        (corpus / "alpha.md").write_text(
            "# Alpha\n\nKangaroos hop across the red outback at dusk.",
            encoding="utf-8")
        second = await index_collection(corpus, collection_dir, config=config)
        by_path = {o.path: o.status for o in second.outcomes}
        assert by_path == {"alpha.md": "indexed", "beta.md": "unchanged"}

        base = KnowledgeBase(collection_dir, config=config)
        outcome = await base.search("kangaroos outback", top_k=3)
        assert outcome.hits and outcome.hits[0].source_path == "alpha.md"
        assert "Kangaroos" in outcome.hits[0].text
        # The replaced version is gone from the index: retrievers
        # return nearest neighbours (a 2-document index answers
        # every query with something), so the property that proves
        # replacement is that NO hit anywhere carries the old text.
        stale = await base.search("zebras savanna", top_k=3)
        assert all("Zebras" not in h.text for h in stale.hits)

        (corpus / "beta.md").unlink()
        third = await index_collection(corpus, collection_dir, config=config)
        assert {o.path: o.status for o in third.outcomes}["beta.md"] == "removed"
        assert third.doc_count == 1

    async def test_embedder_down_records_failure_not_fake_success(
            self, tmp_path, config):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "alpha.md").write_text(
            "# Alpha\n\nZebras graze on the open savanna.", encoding="utf-8")
        episodic._embed_unavailable_until = time.monotonic() + 60.0
        stats = await index_collection(
            corpus, tmp_path / "knowledge" / "default", config=config)
        assert stats.doc_count == 0
        assert stats.outcomes[0].status == "failed"

    async def test_embedder_mismatch_refused_then_force_rebuilds(
            self, indexed, tmp_path):
        corpus, _kd, collection_dir, _stats = indexed
        other = UserConfig(embedding_model="some-other-embedder")
        with pytest.raises(EmbedderMismatchError):
            KnowledgeBase(collection_dir, config=other)
        rebuilt = await index_collection(
            corpus, collection_dir, config=other, force=True)
        assert rebuilt.embedder == "some-other-embedder"
        base = KnowledgeBase(collection_dir, config=other)
        assert base.embedder == "some-other-embedder"


# ---------------------------------------------------------------------------
# Step 1 — the eval set (plan: ~20 question → expected-source pairs)
# ---------------------------------------------------------------------------

EVAL_PAIRS = [
    ("How often should tomato plants be watered?", "gardening.md"),
    ("What does compost add to the soil?", "gardening.md"),
    ("Which herb grows well beside tomatoes?", "gardening.md"),
    ("Which planet has rings made of ice and rock?", "astronomy.md"),
    ("What is the Great Red Spot on Jupiter?", "astronomy.md"),
    ("What can a telescope reveal about a nebula?", "astronomy.md"),
    ("How do I fix a skipping derailleur?", "bicycle.md"),
    ("When should brake pads be replaced?", "bicycle.md"),
    ("How long should sourdough dough proof?", "cooking.md"),
    ("What do you feed a sourdough starter?", "cooking.md"),
    ("How much should an emergency fund hold?", "finance.md"),
    ("What is compound interest?", "finance.md"),
    ("What does a metronome keep steady?", "music.txt"),
    ("What is a chord progression?", "music.txt"),
    ("When was the harbour lighthouse built?", "history.docx"),
    ("What did the ferry trade during the monsoon?", "history.docx"),
    ("How does the canary deployment roll out?", "deploy.py"),
    ("What does the rollback verify before restoring?", "deploy.py"),
    ("What should you put on a grease fire?", "kitchen-safety.pdf"),
    ("Where should sharp knives be stored?", "kitchen-safety.pdf"),
]


class TestEvalSet:
    async def test_eval_set_green(self, indexed, config):
        _corpus, _kd, collection_dir, _stats = indexed
        base = KnowledgeBase(collection_dir, config=config)
        misses = []
        for question, expected in EVAL_PAIRS:
            outcome = await base.search(question, top_k=3)
            sources = [h.source_path for h in outcome.hits]
            if expected not in sources:
                misses.append((question, expected, sources))
        assert not misses, f"eval misses: {misses}"


# ---------------------------------------------------------------------------
# Step 2 — the tool, registry, ceilings
# ---------------------------------------------------------------------------


class TestSearchTool:
    async def test_tool_returns_cited_passages(self, indexed, config):
        _corpus, knowledge_dir, _cd, _stats = indexed
        text = await search_knowledge_base(
            "what does compost add to the soil?",
            knowledge_dir=knowledge_dir, config=config)
        assert "gardening.md" in text
        assert "chunk 0" in text
        assert "last indexed" in text
        assert "nitrogen" in text

    async def test_tool_with_nothing_indexed_answers_plainly(
            self, tmp_path, config):
        text = await search_knowledge_base(
            "anything", knowledge_dir=tmp_path / "empty", config=config)
        assert "No knowledge base has been indexed yet" in text

    async def test_tool_unknown_collection_lists_available(
            self, indexed, config):
        _corpus, knowledge_dir, _cd, _stats = indexed
        text = await search_knowledge_base(
            "anything", "nope", knowledge_dir=knowledge_dir, config=config)
        assert "no knowledge collection named 'nope'" in text
        assert "default" in text

    async def test_available_collections(self, indexed):
        _corpus, knowledge_dir, _cd, _stats = indexed
        assert available_collections(knowledge_dir) == ["default"]

    async def test_degraded_mode_flags_lexical_only(self, indexed, config):
        _corpus, knowledge_dir, _cd, _stats = indexed
        episodic._embed_unavailable_until = time.monotonic() + 60.0
        text = await search_knowledge_base(
            "compost nitrogen soil", knowledge_dir=knowledge_dir,
            config=config)
        assert "lexical matches only" in text
        assert "gardening.md" in text

    async def test_engine_outcome_carries_staleness_marker(
            self, indexed, config):
        _corpus, _kd, collection_dir, stats = indexed
        base = KnowledgeBase(collection_dir, config=config)
        outcome = await base.search("metronome tempo", top_k=3)
        assert outcome.last_indexed == stats.indexed_at
        assert outcome.doc_count == 9
        assert outcome.hits[0].source_path == "music.txt"
        assert outcome.degraded is False


class TestRegistryAndCeilings:
    def test_tool_registered_read_only(self):
        registry = build_tool_registry()
        tool = registry.get_tool("search_knowledge_base")
        assert tool.risk == ToolRisk.READ_ONLY

    def test_researcher_and_planner_ceilings_include_it(self):
        registry = build_tool_registry()
        risk_by_name = {t.name: t.risk for t in registry.get_tools()}
        for agent in ("researcher", "planner"):
            out = apply_tool_ceiling(
                agent, registry.get_definitions(), risk_by_name)
            names = [d["function"]["name"] for d in out]
            assert "search_knowledge_base" in names, agent
