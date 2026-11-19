"""S6: user/relationship profile view + semantic KG retrieval.

Pins the S6 contract from the master-build plan (§4 S6, R9/R11):

* the profile is a VIEW over the KG — derived from the stored
  facts on every call (user entity / Person entities / Preference
  entities), stated before inferred, every item carrying its fact
  id + provenance, inferred items labelled, superseded facts and
  non-profile knowledge (projects, concepts) never shown; an
  empty graph yields an empty profile and an empty briefing,
  never an error; the briefing respects its hard char cap;
* KG retrieval gains an embedding leg on the episodic pattern
  (same embedder, thresholded cosine, model-stamped vectors):
  a paraphrase with NO substring overlap is found semantically,
  substring hits keep priority over semantic-only hits, the
  merge degrades to EXACTLY search_facts() when embeddings are
  unavailable, facts are embedded at extraction time and by a
  bounded lazy backfill (a pre-S6 fact becomes findable), an
  edit drops the stale vector, and the S6 migration adds an
  EMPTY fact_embeddings table to a pre-S6 database without
  touching its rows;
* the S5 assembler consumes the hybrid through the facade: a
  store-attested semantic similarity floors the relevance
  factor, so a zero-token-overlap paraphrase survives scoring;
* the supervisor gains the pull-only get_user_profile tool.

The embedder is a deterministic fake (the episodic tests'
pattern) — no live provider calls. All stores are tmp files.
"""
import json
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.config import UserConfig
from aether_engine.memory import episodic
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import MemoryBackend, NativeMemoryBackend

NOW = int(time.time())
DAY = 86400


# ----------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        yield db


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text, *,
                 ts=NOW, conf=0.9, provenance="stated", importance=0.8,
                 status="active", superseded_by=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, ts, ts))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, ?, ?, ?, ?, ?)",
        (fid, entity_id, text, ts, conf, status, superseded_by,
         provenance, importance))
    conn.commit()
    conn.close()


def _embedding_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT fact_id, model, dimension FROM fact_embeddings").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# The fake embedder: vectors keyed by the EXACT text the KG
# embeds ("<entity>: <fact text>" for facts, the raw string for
# queries) — full deterministic control of every cosine, the
# episodic recall tests' pattern. Unlisted texts land on _FAR,
# orthogonal to every query vector used below.
_DRINK_VEC = [0.94, 0.342, 0.0]  # cosine ~0.94 with [1, 0, 0]
_FAR = [0.0, 1.0, 0.0]
_VECTORS = {
    "hot beverage": [1.0, 0.0, 0.0],
    "User: User's favourite morning drink is filter coffee": _DRINK_VEC,
    "filter coffee": [0.0, 0.0, 1.0],
    # Substring-hit fact whose vector is orthogonal to the query's:
    # it must win by substring priority with NO semantic attestation.
    "User: User loves filter coffee with breakfast": [1.0, 0.0, 0.0],
    # Semantic-only fact for the same query: near-parallel vector.
    "User: User enjoys a hot cup at dawn": [0.1, 0.0, 0.995],
}


async def _fake_embed_text(text, *, config=None, context="external"):
    vec = _VECTORS.get(text)
    return list(vec) if vec is not None else list(_FAR)


@pytest.fixture()
def fake_embedder():
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(side_effect=_fake_embed_text)):
        yield


@pytest.fixture()
def dead_embedder():
    """Embeddings unavailable: every embed returns None."""
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(return_value=None)):
        yield


# ----------------------------------------------------------------------
# Migration: pre-S6 database gains an EMPTY fact_embeddings table
# ----------------------------------------------------------------------

_PRE_S6_SCHEMA = """
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    entity_type TEXT,
    first_seen INTEGER NOT NULL,
    last_updated INTEGER NOT NULL
);
CREATE TABLE facts (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_task_id TEXT,
    timestamp INTEGER NOT NULL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'active',
    superseded_by TEXT,
    provenance TEXT NOT NULL DEFAULT 'inferred',
    importance REAL NOT NULL DEFAULT 0.5,
    last_decayed_at INTEGER,
    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE TABLE aether_schema_meta (
    store TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
INSERT INTO aether_schema_meta (store, version) VALUES ('knowledge_graph', 3);
"""


def test_migration_adds_empty_fact_embeddings_to_pre_s6_db(tmp_path):
    db = tmp_path / "pre_s6.db"
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_S6_SCHEMA)
    conn.execute(
        "INSERT INTO entities VALUES ('e1', 'User', 'Person', ?, ?)", (NOW, NOW))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence, provenance, importance) "
        "VALUES ('f1', 'e1', 'User is called Arindam', 't1', ?, 0.9, 'stated', 0.9)", (NOW,))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence, status, superseded_by) "
        "VALUES ('f2', 'e1', 'User lived in Delhi', 't0', ?, 0.8, 'superseded', 'f1')", (NOW - DAY,))
    conn.commit()
    conn.close()

    # The one-time dedupe pass is not what this test proves.
    kg._DEDUPED_DB_PATHS.add(str(db))
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()

        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "fact_embeddings" in tables
        # Empty until backfill: vectors are derived data, rebuilt
        # from fact text — never migrated, never invented.
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_embeddings").fetchone()[0] == 0
        versions = dict(conn.execute(
            "SELECT store, version FROM aether_schema_meta").fetchall())
        conn.close()
        # The embeddings store versions itself (the episodic
        # pattern); the KG store's version is untouched.
        assert versions["fact_embeddings"] == 1
        assert versions["knowledge_graph"] == 3

        facts = kg.get_all_facts(limit=10, include_history=True)
    assert {f["fact_id"] for f in facts} == {"f1", "f2"}
    assert facts[0]["provenance"] in ("stated", "inferred")


# ----------------------------------------------------------------------
# Semantic retrieval
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semantic_search_finds_paraphrase_without_substring(kg_db, fake_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")
    _insert_fact(kg_db, "f-music", "e2", "User", "Person",
                 "User plays guitar on weekends")

    query = "hot beverage"
    # The premise: the query string appears in NO fact text or
    # entity name — the substring leg alone must miss entirely.
    assert kg.search_facts(query) == []

    results = await kg.search_facts_semantic(query, config=UserConfig())
    assert [r["fact_id"] for r in results] == ["f-drink"]
    assert results[0]["semantic_similarity"] == pytest.approx(0.94, abs=0.01)


@pytest.mark.asyncio
async def test_substring_hit_outranks_semantic_only_hit(kg_db, fake_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-sub", "e1", "User", "Person",
                 "User loves filter coffee with breakfast")
    _insert_fact(kg_db, "f-sem", "e1", "User", "Person",
                 "User enjoys a hot cup at dawn")

    results = await kg.search_facts_semantic("filter coffee", config=UserConfig())
    assert [r["fact_id"] for r in results] == ["f-sub", "f-sem"]
    # The substring winner carries no semantic attestation (its
    # vector is orthogonal to the query's); the semantic runner-up
    # carries its cosine.
    assert "semantic_similarity" not in results[0]
    assert results[1]["semantic_similarity"] == pytest.approx(0.995, abs=0.01)


@pytest.mark.asyncio
async def test_embeddings_unavailable_degrades_to_substring_exactly(kg_db, dead_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-sub", "e1", "User", "Person",
                 "User loves filter coffee with breakfast")
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")

    for query in ("filter coffee", "hot beverage", "User"):
        hybrid = await kg.search_facts_semantic(query, config=UserConfig())
        assert hybrid == kg.search_facts(query)
        assert all("semantic_similarity" not in r for r in hybrid)


@pytest.mark.asyncio
async def test_lazy_backfill_makes_pre_s6_fact_semantically_findable(kg_db, fake_embedder):
    kg.init_kg_db()
    # A pre-S6 fact: stored before embeddings existed, no vector.
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")
    assert _embedding_rows(kg_db) == []

    # The search itself backfills (bounded, oldest first) and the
    # fact becomes findable in the same call.
    results = await kg.search_facts_semantic("hot beverage", config=UserConfig())
    assert [r["fact_id"] for r in results] == ["f-drink"]
    rows = _embedding_rows(kg_db)
    assert [r[0] for r in rows] == ["f-drink"]
    assert rows[0][1] == episodic.resolve_embedder()
    assert rows[0][2] == 3


@pytest.mark.asyncio
async def test_backfill_fact_embeddings_counts_and_active_only(kg_db, fake_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")
    _insert_fact(kg_db, "f-old", "e1", "User", "Person",
                 "User lived in Delhi", status="superseded",
                 superseded_by="f-drink")

    result = await kg.backfill_fact_embeddings(config=UserConfig())
    # Only ACTIVE facts are embedded — superseded history keeps
    # its substring recall and stays out of the embed queue.
    assert result["checked"] == 1
    assert result["backfilled"] == 1
    assert result["failed"] == 0
    assert [r[0] for r in _embedding_rows(kg_db)] == ["f-drink"]

    # Idempotent: a second pass has nothing to do.
    again = await kg.backfill_fact_embeddings(config=UserConfig())
    assert again["backfilled"] == 0
    assert again["checked"] == 1


class _JsonProvider:
    """Extraction test double (the S5 pattern): streams one fixed
    JSON payload for every call."""

    def __init__(self, payload):
        self._text = json.dumps(payload)

    async def call_stream(self, messages):
        class _Chunk:
            def __init__(self, text):
                self.text = text

        yield _Chunk(self._text)


@pytest.mark.asyncio
async def test_extraction_embeds_facts_at_write_time(kg_db, fake_embedder):
    provider = _JsonProvider([
        {"entity_name": "User", "entity_type": "Person",
         "fact": "User's favourite morning drink is filter coffee",
         "confidence": 0.9, "provenance": "stated"},
    ])
    await kg.extract_and_store_facts(
        provider, "My favourite morning drink is filter coffee",
        "Noted.", "task-s6-extract")

    facts = kg.get_all_facts(limit=10)
    assert len(facts) == 1
    # The vector landed with the fact — no backfill needed.
    assert [r[0] for r in _embedding_rows(kg_db)] == [facts[0]["fact_id"]]
    results = await kg.search_facts_semantic(
        "hot beverage", config=UserConfig(), backfill_limit=0)
    assert [r["fact_id"] for r in results] == [facts[0]["fact_id"]]


@pytest.mark.asyncio
async def test_update_fact_drops_stale_embedding(kg_db, fake_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")
    await kg.backfill_fact_embeddings(config=UserConfig())
    assert len(_embedding_rows(kg_db)) == 1

    assert kg.update_fact("f-drink", "User now drinks masala tea instead")
    # The vector described the old text; it is gone, and the fact
    # is a backfill candidate again rather than semantically
    # findable under words it no longer contains.
    assert _embedding_rows(kg_db) == []
    results = await kg.search_facts_semantic(
        "hot beverage", config=UserConfig(), backfill_limit=0)
    assert results == []


@pytest.mark.asyncio
async def test_semantic_leg_respects_superseded_status(kg_db, fake_embedder):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee")
    await kg.backfill_fact_embeddings(config=UserConfig())
    conn = sqlite3.connect(kg_db)
    conn.execute(
        "UPDATE facts SET status = 'superseded', superseded_by = 'f-other' "
        "WHERE id = 'f-drink'")
    conn.commit()
    conn.close()

    # Default search never presents history as current — the
    # semantic leg honours the same filter as the substring leg.
    assert await kg.search_facts_semantic(
        "hot beverage", config=UserConfig(), backfill_limit=0) == []
    history = await kg.search_facts_semantic(
        "hot beverage", config=UserConfig(), backfill_limit=0,
        include_history=True)
    assert [r["fact_id"] for r in history] == ["f-drink"]


@pytest.mark.asyncio
async def test_facade_hybrid_and_base_default():
    """The facade contract: native routes to the KG hybrid; a
    backend without one gets its own search_facts back, unchanged
    (the external-backend contract)."""

    class _PlainBackend(MemoryBackend):
        def search_facts(self, query, limit=10, include_history=False):
            return [{"fact_id": "x", "fact_text": "plain result"}]

    plain = _PlainBackend()
    assert await plain.search_facts_hybrid("anything") == [
        {"fact_id": "x", "fact_text": "plain result"}]
    assert isinstance(NativeMemoryBackend(), MemoryBackend)


@pytest.mark.asyncio
async def test_assembler_consumes_semantic_attestation(kg_db, fake_embedder, tmp_path):
    """S5 x S6 interaction: the assembler's relevance factor takes
    max(lexical, semantic_similarity) — a paraphrase with ZERO
    token overlap is scored in, not dropped."""
    from aether_engine.memory.context_assembler import assemble_memory_context

    kg.init_kg_db()
    _insert_fact(kg_db, "f-drink", "e1", "User", "Person",
                 "User's favourite morning drink is filter coffee",
                 conf=0.9, provenance="stated", importance=0.8)

    result = await assemble_memory_context(
        NativeMemoryBackend(), "hot beverage", config=UserConfig(),
        now=NOW, include_episodes=False,
        curated_path=tmp_path / "absent_curated.md")
    assert [i["id"] for i in result["items"]] == ["f-drink"]
    item = result["items"][0]
    # Lexical relevance of "hot beverage" against this fact is 0;
    # the score exists only because the semantic leg attested it.
    assert item["semantic_similarity"] == pytest.approx(0.94, abs=0.01)
    assert item["relevance"] == pytest.approx(0.94, abs=0.01)
    assert "f-drink" in result["text"]


# ----------------------------------------------------------------------
# Profile view
# ----------------------------------------------------------------------

def _seed_profile(kg_db):
    kg.init_kg_db()
    _insert_fact(kg_db, "f-name", "e-user", "User", "Person",
                 "User is called Arindam", conf=0.95,
                 provenance="stated", importance=0.9)
    _insert_fact(kg_db, "f-tendency", "e-user", "User", "Person",
                 "User tends to work late at night", conf=0.6,
                 provenance="inferred", importance=0.4)
    _insert_fact(kg_db, "f-old", "e-user", "User", "Person",
                 "User lives in Delhi", conf=0.8,
                 provenance="stated", importance=0.8,
                 status="superseded", superseded_by="f-name")
    _insert_fact(kg_db, "f-sister", "e-priya", "Priya", "Person",
                 "Priya is the user's sister", conf=0.9,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-style", "e-style", "Response style", "Preference",
                 "User prefers short, direct answers", conf=0.9,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-proj", "e-proj", "Aether-OS", "Project",
                 "Aether-OS uses SQLite", conf=0.9,
                 provenance="stated", importance=0.8)


def test_profile_view_derives_sections_with_provenance(kg_db):
    from aether_engine.memory import profile as profile_mod

    _seed_profile(kg_db)
    backend = NativeMemoryBackend()
    view = backend.user_profile()

    # Stated before inferred inside the user section.
    assert [i["fact_id"] for i in view["user"]] == ["f-name", "f-tendency"]
    assert view["user"][0]["provenance"] == "stated"
    assert view["user"][1]["provenance"] == "inferred"
    assert list(view["people"]) == ["Priya"]
    assert [i["fact_id"] for i in view["people"]["Priya"]] == ["f-sister"]
    assert [i["fact_id"] for i in view["preferences"]] == ["f-style"]

    briefing = backend.profile_briefing()
    assert "User is called Arindam" in briefing
    assert "[fact f-name]" in briefing
    # Inferred items are labelled; stated items are not.
    tendency_line = next(
        line for line in briefing.splitlines() if "late at night" in line)
    assert "(inferred)" in tendency_line
    name_line = next(
        line for line in briefing.splitlines() if "called Arindam" in line)
    assert "(inferred)" not in name_line
    # People render with their entity name.
    assert "Priya: Priya is the user's sister" in briefing
    # Superseded history and non-profile knowledge never appear.
    assert "lives in Delhi" not in briefing
    assert "Aether-OS uses SQLite" not in briefing
    assert len(briefing) <= profile_mod.PROFILE_BRIEFING_MAX_CHARS


def test_profile_of_empty_graph_is_empty_not_error(kg_db):
    kg.init_kg_db()
    backend = NativeMemoryBackend()
    # SPEC-6B C1 (Wave 6): the contracted reply also carries
    # per-section totals (Rule 2 of the contract note).
    assert backend.user_profile() == {
        "user": [], "people": {}, "preferences": [],
        "totals": {"user": 0, "people": 0, "preferences": 0}}
    assert backend.profile_briefing() == ""


def test_profile_briefing_respects_its_cap(kg_db):
    from aether_engine.memory import profile as profile_mod

    kg.init_kg_db()
    for n in range(30):
        _insert_fact(
            kg_db, f"f-{n:02d}", "e-user", "User", "Person",
            f"User fact number {n} " + "with a fair amount of detail " * 4,
            conf=0.9, provenance="stated", importance=0.8)
    briefing = NativeMemoryBackend().profile_briefing()
    assert briefing
    assert len(briefing) <= profile_mod.PROFILE_BRIEFING_MAX_CHARS
    assert "...[profile truncated to budget]" in briefing


@pytest.mark.asyncio
async def test_get_user_profile_supervisor_tool(kg_db):
    from aether_engine.langgraph.nodes import supervisor

    _seed_profile(kg_db)
    with patch.object(supervisor, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        result = await supervisor._execute_supervisor_memory_tool(
            "get_user_profile", {}, None, "", "", user_config=UserConfig())
    assert "User is called Arindam" in result
    assert "Priya is the user's sister" in result
    assert "lives in Delhi" not in result


@pytest.mark.asyncio
async def test_get_user_profile_tool_empty_graph(kg_db):
    from aether_engine.langgraph.nodes import supervisor

    kg.init_kg_db()
    with patch.object(supervisor, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        result = await supervisor._execute_supervisor_memory_tool(
            "get_user_profile", {}, None, "", "", user_config=UserConfig())
    assert result.startswith("No user profile is stored yet")
