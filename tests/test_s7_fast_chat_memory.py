"""S7: fast-chat memory briefing.

Pins the S7 contract from the master-build plan (§4 S7, R9
fast-chat part):

* fast chat's prompt gains ONE memory section per turn — profile
  digest (from the S6 view) + the S5 assembler's scored recall
  for the user's message (KG facts + past episodes through the
  facade, hybrid candidates per S6) + lessons filling the
  remainder — all sharing the ONE sub-budget
  (fast_chat_memory_token_budget, passed INTO the assembler's
  call, never a second scoring path);
* the section is honest: a header stating this is long-term
  memory that can be incomplete or out of date, inferred digest
  items labelled, every scored item carrying its source id;
* memory is additive, never a new failure mode: toggle off,
  empty memory, a dead embedder (for a paraphrase only the
  semantic leg could find), or a raising facade all yield the
  pre-S7 prompt composition byte for byte;
* voice parity is structural: the voice talker calls
  run_fast_chat, and the briefing lives inside run_fast_chat —
  there is no second prompt builder to drift.

All stores are tmp files; the embedder is the S6 deterministic
fake (or dead) — no live provider calls.
"""
import logging
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph.fast_chat import (
    _SYSTEM_PROMPT,
    fast_chat_memory_briefing,
    run_fast_chat,
)
from aether_engine.memory import episodic, learned
from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.context_assembler import estimate_tokens
from aether_engine.providers.base import StreamChunk

NOW = int(time.time())


# ----------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()
        yield db


@pytest.fixture()
def epi_db(tmp_path):
    mem_db = tmp_path / "episodes.db"
    emb_db = tmp_path / "embeddings.db"
    orig_get_conn = episodic._get_conn

    def fake_get_conn(db_path=None):
        if db_path is None or str(db_path) == str(episodic.DB_PATH):
            return orig_get_conn(mem_db)
        if str(db_path) == str(episodic.EMBEDDINGS_DB_PATH):
            return orig_get_conn(emb_db)
        return orig_get_conn(db_path)

    with patch.object(episodic, "_get_conn", fake_get_conn), \
            patch.object(episodic, "DB_PATH", mem_db), \
            patch.object(episodic, "EMBEDDINGS_DB_PATH", emb_db):
        yield mem_db


@pytest.fixture()
def no_curated(tmp_path):
    """Assembly must not read a curated document some other test
    wrote into the shared sandbox home."""
    with patch("aether_engine.memory.consolidation.MEMORY_DOCUMENT_PATH",
               tmp_path / "curated_memory.md"):
        yield


@pytest.fixture()
def learned_md(tmp_path):
    path = tmp_path / "learned.md"
    with patch.object(learned, "LEARNED_MD_PATH", path):
        yield path


@pytest.fixture()
def dead_embedder():
    """Embeddings unavailable: every embed returns None (the S6
    fixture, extended to episodic's internal _embed_text, which
    store/query call directly) — substring/BM25 legs must carry
    recall alone."""
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(return_value=None)), \
            patch.object(episodic, "_embed_text",
                         new=AsyncMock(return_value=None)):
        yield


# The fake embedder (S6 pattern): vectors keyed by the EXACT text
# the KG embeds ("<entity>: <fact text>") and the raw query.
_DRINK_VEC = [0.94, 0.342, 0.0]  # cosine ~0.94 with [1, 0, 0]
_FAR = [0.0, 1.0, 0.0]
_VECTORS = {
    "hot beverage": [1.0, 0.0, 0.0],
    "Filter coffee: User's favourite morning drink is filter coffee":
        _DRINK_VEC,
}


async def _fake_embed_text(text, *, config=None, context="external"):
    vec = _VECTORS.get(text)
    return list(vec) if vec is not None else list(_FAR)


@pytest.fixture()
def fake_embedder():
    with patch.object(episodic, "embed_text",
                      new=AsyncMock(side_effect=_fake_embed_text)):
        yield


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text, *,
                 ts=NOW, conf=0.9, provenance="stated", importance=0.8):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, ts, ts))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, ?, 'active', NULL, ?, ?)",
        (fid, entity_id, text, ts, conf, provenance, importance))
    conn.commit()
    conn.close()


def _seed_profile(db_path):
    """The user, a person, a preference, and one query-relevant
    fact on a Concept entity (knowledge, not profile)."""
    _insert_fact(db_path, "f-name", "e-user", "User", "Person",
                 "User's name is Arindam Roy", conf=0.95, importance=0.9)
    _insert_fact(db_path, "f-maa", "e-maa", "Maa", "Person",
                 "Maa is the user's mother")
    _insert_fact(db_path, "f-pref", "e-pref", "Communication style",
                 "Preference", "User prefers concise, direct replies")
    _insert_fact(db_path, "f-drink", "e-coffee", "Filter coffee", "Concept",
                 "User's favourite morning drink is filter coffee")


class ChunkProvider:
    """Records the messages it was shown; yields fixed chunks
    (the test_epic1_fast_chat pattern)."""

    def __init__(self, chunks=("answer",)):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


async def _collect(agen):
    return [event async for event in agen]


def _pre_s7_messages(prompt):
    """The exact message list run_fast_chat built before S7 for a
    history-free turn with an empty calendar: persona + user."""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


# ----------------------------------------------------------------------
# The briefing carries facts + profile
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefing_carries_relevant_fact_and_profile_digest(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    _seed_profile(kg_db)

    briefing = await fast_chat_memory_briefing(
        "what is my favourite morning drink?", UserConfig())

    assert briefing.startswith("What Aether remembers")
    # The assembler leg: the query-relevant fact, with its id.
    assert "[fact f-drink]" in briefing
    assert "filter coffee" in briefing
    # The profile digest: stated name fact, the person's name,
    # the preference.
    assert "About the user: User's name is Arindam Roy" in briefing
    assert "People in the user's life: Maa" in briefing
    assert "User prefers concise, direct replies" in briefing
    # The whole section respects the default sub-budget.
    assert estimate_tokens(briefing) <= 400


@pytest.mark.asyncio
async def test_run_fast_chat_prompt_carries_memory_section(
        kg_db, epi_db, no_curated, learned_md, dead_embedder, caplog):
    _seed_profile(kg_db)
    provider = ChunkProvider()

    with caplog.at_level(logging.DEBUG,
                         logger="aether_engine.langgraph.fast_chat"):
        events = await _collect(run_fast_chat(
            "what is my favourite morning drink?", provider, [],
            user_config=UserConfig()))

    assert events[-1].type == EventType.TASK_COMPLETED
    messages = provider.calls[0]
    memory = [m for m in messages
              if m["role"] == "system" and "What Aether remembers" in m["content"]]
    assert len(memory) == 1
    assert "[fact f-drink]" in memory[0]["content"]
    assert "Arindam Roy" in memory[0]["content"]
    # The memory section rides as a system message BEFORE the
    # user turn it was assembled for.
    assert messages.index(memory[0]) < len(messages) - 1
    assert messages[-1] == {
        "role": "user", "content": "what is my favourite morning drink?"}
    # Latency honesty: the assembly duration is on the debug log.
    assert any("Fast-chat memory briefing" in r.message and "ms" in r.message
               for r in caplog.records)


# ----------------------------------------------------------------------
# Budget discipline
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_briefing_respects_sub_budget(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    padding = ("coffee " * 12).strip()
    for i in range(30):
        _insert_fact(kg_db, f"f-note-{i:02d}", f"e-note-{i:02d}",
                     f"Coffee note {i:02d}", "Concept",
                     f"coffee note {i:02d}: {padding}")
    config = UserConfig(fast_chat_memory_token_budget=120)

    briefing = await fast_chat_memory_briefing("coffee", config)

    assert briefing
    assert "[fact f-note-" in briefing
    assert estimate_tokens(briefing) <= 120


# ----------------------------------------------------------------------
# Fallbacks: the pre-S7 prompt, byte for byte
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_memory_means_pre_s7_prompt(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    assert await fast_chat_memory_briefing("hello there", UserConfig()) == ""

    provider = ChunkProvider()
    await _collect(run_fast_chat(
        "hello there", provider, [], user_config=UserConfig()))
    assert provider.calls[0] == _pre_s7_messages("hello there")


@pytest.mark.asyncio
async def test_toggle_off_is_byte_identical(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    _seed_profile(kg_db)
    config = UserConfig(fast_chat_memory_enabled=False)

    assert await fast_chat_memory_briefing(
        "what is my favourite morning drink?", config) == ""

    provider = ChunkProvider()
    await _collect(run_fast_chat(
        "what is my favourite morning drink?", provider, [],
        user_config=config))
    assert provider.calls[0] == _pre_s7_messages(
        "what is my favourite morning drink?")


@pytest.mark.asyncio
async def test_facade_failure_falls_back(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    _seed_profile(kg_db)
    from aether_engine.memory import facade

    with patch.object(facade, "backend_or_none",
                      side_effect=RuntimeError("backend exploded")):
        assert await fast_chat_memory_briefing(
            "what is my favourite morning drink?", UserConfig()) == ""

    from aether_engine.memory import context_assembler

    with patch.object(context_assembler, "assemble_memory_context",
                      new=AsyncMock(side_effect=RuntimeError("assembly broke"))):
        assert await fast_chat_memory_briefing(
            "what is my favourite morning drink?", UserConfig()) == ""

    provider = ChunkProvider()
    with patch.object(facade, "backend_or_none",
                      side_effect=RuntimeError("backend exploded")):
        events = await _collect(run_fast_chat(
            "hello", provider, [], user_config=UserConfig()))
    assert events[-1].type == EventType.TASK_COMPLETED
    assert provider.calls[0] == _pre_s7_messages("hello")


# ----------------------------------------------------------------------
# The semantic leg reaches fast chat — and degrades honestly
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_semantic_paraphrase_reaches_fast_chat(
        kg_db, epi_db, no_curated, learned_md, fake_embedder):
    # The fact sits on a Concept entity: the profile digest
    # cannot carry it, so only the assembler's semantic leg can.
    _insert_fact(kg_db, "f-drink", "e-coffee", "Filter coffee", "Concept",
                 "User's favourite morning drink is filter coffee")

    briefing = await fast_chat_memory_briefing("hot beverage", UserConfig())

    assert "[fact f-drink]" in briefing


@pytest.mark.asyncio
async def test_embedder_down_paraphrase_falls_back(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    _insert_fact(kg_db, "f-drink", "e-coffee", "Filter coffee", "Concept",
                 "User's favourite morning drink is filter coffee")

    # No substring overlap and no semantic leg: nothing scored
    # in, no section, and the chat prompt is the pre-S7 one.
    assert await fast_chat_memory_briefing("hot beverage", UserConfig()) == ""

    provider = ChunkProvider()
    await _collect(run_fast_chat(
        "hot beverage", provider, [], user_config=UserConfig()))
    assert provider.calls[0] == _pre_s7_messages("hot beverage")


# ----------------------------------------------------------------------
# Episodes + lessons
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_episode_surfaces_for_matching_message(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    await episodic.store_episode(
        "ep-gimbal", "Fix the gimbal motor noise",
        "Gimbal motor replaced", ["repair"], "The gimbal runs quietly now.")

    # The episodic FTS leg ANDs its query tokens, so the message
    # uses the episode's own words — the BM25 leg must carry this
    # with embeddings dead.
    briefing = await fast_chat_memory_briefing(
        "gimbal motor noise", UserConfig())

    assert "[episode ep-gimbal]" in briefing


@pytest.mark.asyncio
async def test_lessons_fill_the_section(
        kg_db, epi_db, no_curated, learned_md, dead_embedder):
    learned_md.write_text(
        "# Lessons\n\n- Keep replies short on weekday mornings.\n"
        "- Always confirm before sending email.\n",
        encoding="utf-8")

    briefing = await fast_chat_memory_briefing("good morning", UserConfig())

    assert "Lessons learned from earlier sessions" in briefing
    assert "Keep replies short on weekday mornings." in briefing
    assert estimate_tokens(briefing) <= 400


# ----------------------------------------------------------------------
# Config surface
# ----------------------------------------------------------------------

def test_config_defaults_and_round_trip():
    config = UserConfig()
    assert config.fast_chat_memory_enabled is True
    assert config.fast_chat_memory_token_budget == 400

    custom = UserConfig(fast_chat_memory_enabled=False,
                        fast_chat_memory_token_budget=250)
    restored = UserConfig.from_dict(custom.to_dict())
    assert restored.fast_chat_memory_enabled is False
    assert restored.fast_chat_memory_token_budget == 250
