"""Epic 4, E4-F09 (addendum E4-10, owner-approved 30 Oct 2026):
the learned/consolidated memory-document layer.

The promise (aether-os-plan-addendum-gaps.md, Epic 4 — Memory):
  * "learn loop: explicit lessons extracted at task end into a
    deduped, redacted, capped learned.md, injected into the next
    session's briefing." (omp #29)
  * "Memory consolidation: a periodic consolidation pass turning
    accumulated episodic/KG rows into a readable curated memory
    document (secret-redacted, token-capped)." (omp #33)

Pre-fix there was no implementation at all: no learned.md writer,
no briefing injection, no consolidation pass — the memory
scheduler ran retention pruning only. These tests drive the
promised surfaces (facade task-end learn path, supervisor session
briefing, scheduler consolidation job) with real inputs.
"""
import json
import sqlite3
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from aether_engine.providers.base import StreamChunk

SECRET = "sk-TESTSECRET1234567890"


# ----------------------------------------------------------------------
# Fixtures: isolated native stores + isolated document paths
# ----------------------------------------------------------------------

@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch("aether_engine.memory.knowledge_graph.DB_PATH", db):
        yield db


@pytest.fixture()
def epi_db(tmp_path):
    from aether_engine.memory import episodic

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
def doc_paths(tmp_path):
    """Point the two memory documents at tmp files and silence the
    modules' audit loggers into mocks the tests can inspect."""
    learned_md = tmp_path / "learned.md"
    curated_md = tmp_path / "curated_memory.md"
    with patch("aether_engine.memory.learned.LEARNED_MD_PATH", learned_md), \
            patch("aether_engine.memory.consolidation.MEMORY_DOCUMENT_PATH", curated_md), \
            patch("aether_engine.memory.learned._audit_logger") as learned_audit, \
            patch("aether_engine.memory.consolidation._audit_logger") as doc_audit:
        yield {
            "learned_md": learned_md,
            "curated_md": curated_md,
            "learned_audit": learned_audit,
            "doc_audit": doc_audit,
        }


@pytest.fixture(autouse=True)
def _clear_backend_cache():
    from aether_engine.memory import facade

    facade._BACKEND_CACHE.clear()
    yield
    facade._BACKEND_CACHE.clear()


class LearnProvider:
    """Extraction stub covering both extraction engines: the lesson
    prompt gets the configured lessons; the KG reconcile prompt (it
    lists OLD facts) gets one superseding pair; fact extraction
    answers Kolkata/Mumbai by sniffing the conversation."""

    def __init__(self, lessons=()):
        self.lessons = list(lessons)

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        blob = json.dumps(messages)
        if "Lesson Extraction" in blob:
            yield StreamChunk(text=json.dumps(self.lessons), finish_reason="stop")
        elif "OLD facts:" in blob:
            yield StreamChunk(text='[{"old": 0, "new": 0}]', finish_reason="stop")
        elif "Mumbai" in blob:
            yield StreamChunk(
                text='[{"entity_name": "User", "entity_type": "Person", '
                     '"fact": "User lives in Mumbai", "confidence": 0.95}]',
                finish_reason="stop")
        else:
            yield StreamChunk(
                text='[{"entity_name": "User", "entity_type": "Person", '
                     '"fact": "User lives in Kolkata", "confidence": 0.9}]',
                finish_reason="stop")


# ----------------------------------------------------------------------
# The learn loop — task end writes learned.md
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_end_lessons_are_extracted_into_learned_md(doc_paths):
    from aether_engine.memory.facade import NativeMemoryBackend

    provider = LearnProvider(lessons=[
        "Always run the test suite before committing.",
        "The owner prefers concise summaries.",
    ])
    backend = NativeMemoryBackend()
    added = await backend.learn_lessons(
        provider, "Build the thing", "Built it; tests passed.", "task-1")

    assert added == [
        "Always run the test suite before committing.",
        "The owner prefers concise summaries.",
    ]
    text = doc_paths["learned_md"].read_text(encoding="utf-8")
    assert "Always run the test suite before committing." in text
    assert "The owner prefers concise summaries." in text

    from aether_engine.memory import learned

    assert learned.load_lessons() == added
    events = [c.args[0] for c in doc_paths["learned_audit"].log_event.call_args_list]
    assert "LESSONS_RECORDED" in events


@pytest.mark.asyncio
async def test_lessons_deduped_across_tasks(doc_paths):
    from aether_engine.memory.facade import NativeMemoryBackend

    backend = NativeMemoryBackend()
    await backend.learn_lessons(
        LearnProvider(lessons=["Run tests before committing."]),
        "task one prompt", "done one", "task-1")
    # Same lesson with different case/whitespace, plus one new one.
    added = await backend.learn_lessons(
        LearnProvider(lessons=["  run tests   before committing. ",
                               "Prefer small commits."]),
        "task two prompt", "done two", "task-2")

    assert added == ["Prefer small commits."]
    from aether_engine.memory import learned

    assert learned.load_lessons() == [
        "Run tests before committing.", "Prefer small commits."]


@pytest.mark.asyncio
async def test_lessons_are_secret_redacted_before_they_touch_disk(doc_paths):
    from aether_engine.memory.facade import NativeMemoryBackend

    backend = NativeMemoryBackend()
    added = await backend.learn_lessons(
        LearnProvider(lessons=[f"The deploy key is {SECRET} — never print it."]),
        "deploy the thing", "deployed", "task-1")

    raw = doc_paths["learned_md"].read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "[REDACTED]" in raw
    assert all(SECRET not in lesson for lesson in added)


@pytest.mark.asyncio
async def test_learned_md_is_capped_oldest_dropped(doc_paths):
    from aether_engine.memory import learned

    with patch.object(learned, "MAX_LESSONS", 5), \
            patch.object(learned, "MAX_LEARNED_CHARS", 100_000):
        for i in range(4):
            learned.record_lessons([f"Lesson number {i}a.", f"Lesson number {i}b."],
                                   task_id=f"t{i}")
        lessons = learned.load_lessons()

    assert len(lessons) == 5
    # The newest survive; the oldest were dropped to hold the cap.
    assert lessons[-1] == "Lesson number 3b."
    assert "Lesson number 0a." not in lessons


@pytest.mark.asyncio
async def test_lesson_extraction_failure_is_best_effort(doc_paths):
    from aether_engine.memory.facade import NativeMemoryBackend

    class BrokenProvider:
        async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
            raise RuntimeError("provider down")
            yield  # pragma: no cover — makes this an async generator

    backend = NativeMemoryBackend()
    added = await backend.learn_lessons(
        BrokenProvider(), "a real prompt", "a real response", "task-x")
    assert added == []
    assert not doc_paths["learned_md"].exists()


@pytest.mark.asyncio
async def test_fast_chat_exchange_records_lessons(doc_paths, kg_db, epi_db):
    from aether_engine.langgraph.fast_chat import learn_fast_chat_exchange

    await learn_fast_chat_exchange(
        "chat-task-9", "Please refactor the parser",
        "Refactored; the parser now streams tokens.",
        LearnProvider(lessons=["The parser streams tokens; do not buffer whole files."]),
        None)

    from aether_engine.memory import learned

    assert learned.load_lessons() == [
        "The parser streams tokens; do not buffer whole files."]


# ----------------------------------------------------------------------
# Briefing injection — the next session sees the lessons
# ----------------------------------------------------------------------

def test_lessons_injected_into_next_session_briefing(doc_paths):
    from aether_engine.langgraph.nodes.supervisor import _supervisor_system_prompt
    from aether_engine.memory import learned

    learned.record_lessons(["Always run the test suite before committing."],
                           task_id="task-1")
    prompt = _supervisor_system_prompt("do the next thing", "the plan",
                                       ["planner", "coder"])
    assert "Always run the test suite before committing." in prompt
    assert "learned.md" in prompt


def test_no_lessons_no_briefing_block(doc_paths):
    from aether_engine.langgraph.nodes.supervisor import _supervisor_system_prompt
    from aether_engine.memory import learned

    assert learned.learned_briefing() == ""
    prompt = _supervisor_system_prompt("hello", "plan", ["planner"])
    assert "learned.md" not in prompt


def test_briefing_render_is_capped(doc_paths):
    from aether_engine.memory import learned

    learned.record_lessons([f"Lesson {i}: " + "x" * 120 for i in range(50)],
                           task_id="t")
    block = learned.learned_briefing()
    assert 0 < len(block) <= learned.BRIEFING_MAX_CHARS + 40
    assert "truncated" in block


# ----------------------------------------------------------------------
# Consolidation — periodic curated memory document
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consolidation_writes_curated_document(doc_paths, kg_db, epi_db):
    from aether_engine.memory import episodic
    from aether_engine.memory.consolidation import consolidate_memory_document
    from aether_engine.memory.facade import NativeMemoryBackend

    backend = NativeMemoryBackend()
    provider = LearnProvider()
    await backend.learn(provider, "I live in Kolkata", "Noted!", "task-c1")
    # A later move supersedes the Kolkata fact (reconcile pairs old 0 -> new 0).
    await backend.learn(provider, "I moved to Mumbai last week", "Congrats!", "task-c2")
    await episodic.store_episode(
        "task-c1", "Set up the project", "Project scaffolded",
        ["task", "completed"], "Scaffold is in place.")

    result = consolidate_memory_document()

    assert result["written"] is True
    assert result["facts"] == 1  # only the ACTIVE fact is curated
    assert result["episodes"] == 1
    doc = doc_paths["curated_md"].read_text(encoding="utf-8")
    assert "User lives in Mumbai" in doc
    # A superseded fact must not be resurrected by consolidation.
    assert "User lives in Kolkata" not in doc
    assert "Project scaffolded" in doc
    events = [c.args[0] for c in doc_paths["doc_audit"].log_event.call_args_list]
    assert "MEMORY_DOCUMENT_CONSOLIDATED" in events


@pytest.mark.asyncio
async def test_consolidation_redacts_secrets_and_caps(doc_paths, kg_db, epi_db):
    from aether_engine.memory import knowledge_graph as kg
    from aether_engine.memory.consolidation import consolidate_memory_document

    kg.init_kg_db()
    conn = sqlite3.connect(kg.DB_PATH)
    now = int(time.time())
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES ('e1', 'Infra', 'Concept', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) "
        "VALUES ('f1', 'e1', ?, 'seed', ?, 0.9)",
        (f"The deploy key is {SECRET} and rotates monthly", now))
    conn.commit()
    conn.close()

    from aether_engine.memory import consolidation

    with patch.object(consolidation, "MAX_DOCUMENT_CHARS", 220):
        result = consolidate_memory_document()
        doc = doc_paths["curated_md"].read_text(encoding="utf-8")

    assert result["written"] is True
    assert SECRET not in doc
    assert len(doc) <= 220
    assert "Truncated" in doc


# ----------------------------------------------------------------------
# Scheduler wiring — periodic, like pruning
# ----------------------------------------------------------------------

def test_scheduler_registers_weekly_consolidation_job(tmp_path, doc_paths):
    from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

    class FakeTrigger:
        def __init__(self, **kwargs):
            self.interval = timedelta(**kwargs)

    manager = CapabilitySchedulerManager(db_path=tmp_path / "cap.db")
    fake_scheduler = MagicMock()
    fake_scheduler.running = True
    manager.scheduler = fake_scheduler

    with patch("aether_engine.scheduler.capability_jobs.HAS_APSCHEDULER", True), patch(
        "aether_engine.scheduler.capability_jobs.IntervalTrigger", FakeTrigger
    ):
        manager._register_memory_consolidation_job()

    fake_scheduler.add_job.assert_called_once()
    _, kwargs = fake_scheduler.add_job.call_args
    assert kwargs["id"] == "memory_consolidation_weekly"
    assert kwargs["replace_existing"] is True
    assert kwargs["trigger"].interval == timedelta(weeks=1)


@pytest.mark.asyncio
async def test_scheduler_run_consolidation_now_writes_document(
        tmp_path, doc_paths, kg_db, epi_db):
    from aether_engine.memory.facade import NativeMemoryBackend
    from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

    backend = NativeMemoryBackend()
    await backend.learn(LearnProvider(), "I live in Kolkata", "Noted!", "task-s1")

    manager = CapabilitySchedulerManager(db_path=tmp_path / "cap.db")
    result = manager.run_memory_consolidation_now()

    assert result["written"] is True
    doc = doc_paths["curated_md"].read_text(encoding="utf-8")
    assert "User lives in Kolkata" in doc
