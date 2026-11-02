"""Epic 8, N01: repeated writes must not vanish from the warning.

Rewind is STATE-ONLY (grill Q2): the confirm modal and the rewind
response list the files written after the chosen point as "will NOT
be reverted" — the owner relies on that list to know what a rewind
won't undo. The manifest used to track only UNIQUE cumulative paths
per point, and the warning was the set difference between the final
and target points' lists. A file written in an early step and written
AGAIN after the boundary sits in both sets, so the subtraction
removed it — even though its current content postdates the boundary
and survives the rewind.

The fix records per-point write EVENTS (every landed write, repeats
included) alongside the cumulative list, and computes the warning
from the events after the boundary (unioned with the cumulative
difference, which keeps manifests recorded before events existed —
they carry no events — exactly as accurate as they ever were).

Layers covered:
  * langgraph.executor — the tracker notes every write as an event
    while the cumulative list stays deduped (unit + a real graph
    run whose planner writes the same file in two tool cycles).
  * rewind.manifest — events persist per point; a pre-events database
    (no column) migrates and reads events as [].
  * rewind.service — the warning itself over recorded trails, plus
    undo carrying the events back through the staging backup.

All providers are scripted stubs (house pattern): no network.
"""
import sqlite3
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType
from aether_engine.langgraph import executor as executor_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service
from aether_engine.tools.registry import build_tool_registry

# ---------------------------------------------------------------------------
# Fixtures / stubs (mirroring test_epic8_rewind_execution.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest + rewind backups,
    which share its DB) isolated to a tmp memory.db."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


@pytest.fixture
def graph_env(store_env, monkeypatch):
    """Isolate a full executor run (parity pattern): tmp checkpoint DB,
    no real memory writes on completion, no artifact files on disk."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", store_env / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    from aether_engine.langgraph.nodes import planner as planner_module
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "test/artifact.txt")
    # The subprocess sandbox needs Windows Job Objects (pywin32),
    # absent on this Linux VM; dispatching to run_worker in-process
    # exercises the identical worker code path, minus isolation.
    from aether_engine.workers import sandbox
    from aether_worker.__main__ import run_worker

    async def _in_process_worker(tool_name, args, task_class="standard",
                                 audit_logger=None, task_classes=None):
        return await run_worker({"tool_name": tool_name, "args": args})

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)
    return store_env


def _routing_chunk():
    return StreamChunk(
        text='{"next": "planner", "reason": "test routing", "briefing": ""}',
        finish_reason="stop",
    )


class RepeatWritingProvider:
    """Full-graph stub: the planner writes the SAME file in two
    separate tool cycles (content "one", then content "two"), then
    answers with final text."""

    def __init__(self, target_path):
        self._planner_calls = 0
        self._target_path = target_path

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls <= 2:
                yield StreamChunk(
                    tool_calls=[ToolCall(
                        name="write_file",
                        args={
                            "path": self._target_path,
                            "content": f"version {self._planner_calls}",
                        },
                        call_id=f"call-write-{self._planner_calls}",
                    )],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(
                    text="Wrote the file twice.", finish_reason="stop")
            return
        yield _routing_chunk()


async def _collect(agen):
    return [event async for event in agen]


async def _run_task(graph_env, sid, provider, task_id, prompt="please write"):
    """Run a real graph task the way the websocket layer drives it:
    the user row is saved before the run, the assistant row after
    TASK_COMPLETED. Returns the manifest entries."""
    from aether_engine.config import UserConfig

    session_store.add_message(sid, "user", prompt)
    events = await _collect(executor_module.run_langgraph_task(
        prompt,
        provider,
        tools=build_tool_registry(),
        task_id=task_id,
        request_id=f"req-{task_id}",
        session_id=sid,
        user_config=UserConfig(approval_mode="write"),
        workspace_roots=[str(graph_env)],
    ))
    assert events[-1].type == EventType.TASK_COMPLETED
    session_store.add_message(sid, "assistant", "Done.")
    return rewind_manifest.get_entries(sid, task_id)


# ---------------------------------------------------------------------------
# Executor tracker: events are noted per write, cumulative stays unique
# ---------------------------------------------------------------------------


def test_tracker_notes_every_write_as_an_event():
    tracker = executor_module._new_manifest_tracker()
    msg = {"name": "write_file", "args": {"path": "notes/x.txt"}}
    executor_module._note_manifest_file_write(tracker, msg, False)
    executor_module._note_manifest_file_write(tracker, msg, False)
    # The cumulative list is unchanged in shape: unique paths.
    assert tracker["files_written"] == ["notes/x.txt"]
    # The event buffer keeps BOTH writes — the repeat is the signal
    # the warning needs.
    assert tracker["pending_write_events"] == ["notes/x.txt", "notes/x.txt"]
    # Failed writes and non-write tools are not events.
    executor_module._note_manifest_file_write(tracker, msg, True)
    executor_module._note_manifest_file_write(
        tracker, {"name": "read_file", "args": {"path": "y.txt"}}, False)
    assert tracker["pending_write_events"] == ["notes/x.txt", "notes/x.txt"]
    assert tracker["files_written"] == ["notes/x.txt"]


# ---------------------------------------------------------------------------
# Manifest store: events persist per point; pre-events DBs migrate
# ---------------------------------------------------------------------------


def test_record_point_persists_write_events_per_point(store_env):
    sid = session_store.create_session("Events")
    rewind_manifest.record_point(
        sid, "task-1", 0, "cp-a", "Before this task began", [])
    rewind_manifest.record_point(
        sid, "task-1", 1, "cp-b", "After planner", ["a.txt"],
        write_events=["a.txt"])
    rewind_manifest.record_point(
        sid, "task-1", 2, "cp-c", "Task complete", ["a.txt"],
        write_events=["a.txt"])

    entries = rewind_manifest.get_entries(sid, "task-1")
    assert entries[0]["write_events"] == []
    assert entries[1]["write_events"] == ["a.txt"]
    # The repeat write at step 2 is recorded as an event even though
    # the cumulative list did not change.
    assert entries[2]["write_events"] == ["a.txt"]
    assert entries[2]["files_written"] == ["a.txt"]


def test_pre_events_database_migrates_and_reads_empty_events(store_env):
    """A memory.db whose rewind_manifest predates write events (no
    write_events_json column) gains the column on init; its rows
    read back with write_events == [] and stay fully usable."""
    sid = session_store.create_session("Legacy")
    db_path = store_env / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE rewind_manifest (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                checkpoint_id TEXT NOT NULL,
                label TEXT NOT NULL,
                file_writes_json TEXT NOT NULL DEFAULT '[]',
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
                    ON DELETE CASCADE
            )
        """)
        conn.execute(
            "INSERT INTO rewind_manifest "
            "(id, session_id, task_id, step_index, checkpoint_id, label, "
            "file_writes_json, timestamp) VALUES "
            "('legacy-1', ?, 'task-old', 0, 'cp-a', "
            "'Before this task began', '[]', 1)",
            (sid,),
        )
        conn.commit()

    entries = rewind_manifest.get_entries(sid, "task-old")
    assert len(entries) == 1
    assert entries[0]["files_written"] == []
    assert entries[0]["write_events"] == []
    # New points record normally against the migrated table.
    rewind_manifest.record_point(
        sid, "task-old", 1, "cp-b", "After planner", ["a.txt"],
        write_events=["a.txt"])
    assert rewind_manifest.get_entries(sid, "task-old")[1]["write_events"] == ["a.txt"]


# ---------------------------------------------------------------------------
# The warning itself
#
# Recorded trails are the unit the warning reads: a specialist's
# tool cycles can stream into a single manifest point, so the
# warning semantics are exercised on recorded points directly (the
# audit's own probe method) — the graph-run test below covers the
# recording side end to end.
# ---------------------------------------------------------------------------


def _record_trail(sid, task_id, points):
    """Record a synthetic trail: points are (files_written,
    write_events) per step, step 0 first."""
    for step, (files, events) in enumerate(points):
        rewind_manifest.record_point(
            sid, task_id, step, "", f"Step {step}", files,
            write_events=events)


def _session_with_turn(sid):
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")


def test_repeat_write_after_boundary_appears_in_warning(store_env):
    """THE N01 repro: X written at step 1, X written AGAIN at step 2;
    rewinding to step 1 (between the two writes) must list X — its
    current content postdates the boundary and will not be reverted.
    Pre-fix, the cumulative set difference removed X entirely."""
    sid = session_store.create_session("Repeat write")
    _session_with_turn(sid)
    _record_trail(sid, "task-1", [
        ([], []),
        (["x.txt"], ["x.txt"]),
        (["x.txt"], ["x.txt"]),          # the repeat write
        (["x.txt", "y.txt"], ["y.txt"]),
    ])

    result = rewind_service.stage_rewind(sid, "task-1", 1, "")
    assert result["ok"] is True
    assert result["files_not_reverted"] == ["x.txt", "y.txt"]

    # Undo restores the trail whole — write events included, so a
    # later rewind's warning is computed from the restored record.
    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    restored = rewind_manifest.get_entries(sid, "task-1")
    assert [e["write_events"] for e in restored] == \
        [[], ["x.txt"], ["x.txt"], ["y.txt"]]
    assert [e["files_written"] for e in restored] == \
        [[], ["x.txt"], ["x.txt"], ["x.txt", "y.txt"]]


def test_only_post_boundary_writes_are_warned(store_env):
    """Pins the boundary semantics: a file whose LAST write is at or
    before the point is not listed; a file written only after it is.
    Rewinding to step 2 of the repeat trail warns about y.txt only —
    x.txt's repeat write is AT the boundary point, not after it."""
    sid = session_store.create_session("Boundary")
    _session_with_turn(sid)
    _record_trail(sid, "task-1", [
        ([], []),
        (["x.txt"], ["x.txt"]),
        (["x.txt"], ["x.txt"]),
        (["x.txt", "y.txt"], ["y.txt"]),
    ])

    result = rewind_service.stage_rewind(sid, "task-1", 2, "")
    assert result["ok"] is True
    assert result["files_not_reverted"] == ["y.txt"]
    assert rewind_service.undo_staged(sid)["restored"] is True


def test_rewind_to_step_zero_warns_repeat_written_file_once(store_env):
    """Rewinding past BOTH writes lists the file exactly once — the
    warning is a set of paths, however many events name it."""
    sid = session_store.create_session("Repeat write")
    _session_with_turn(sid)
    _record_trail(sid, "task-1", [
        ([], []),
        (["x.txt"], ["x.txt"]),
        (["x.txt"], ["x.txt"]),
    ])

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert result["ok"] is True
    assert result["files_not_reverted"] == ["x.txt"]
    assert rewind_service.undo_staged(sid)["restored"] is True


@pytest.mark.asyncio
async def test_graph_task_records_per_point_write_events(graph_env):
    """End to end through a real graph run: the planner writes the
    same file in two tool cycles; the recorded trail carries BOTH
    writes as events while the cumulative list stays unique."""
    sid = session_store.create_session("Repeat write")
    target = graph_env / "notes" / "out.txt"
    entries = await _run_task(
        graph_env, sid, RepeatWritingProvider(str(target)), "task-repeat")

    assert target.read_text(encoding="utf-8") == "version 2"
    # Cumulative list: still the unique paths, exactly as before.
    assert entries[-1]["files_written"] == [str(target)]
    # Events: every landed write is on the trail — the file was
    # written twice, so it appears twice across the points.
    all_events = [p for e in entries for p in e["write_events"]]
    assert all_events == [str(target), str(target)]


# ---------------------------------------------------------------------------
# Legacy manifests (points recorded before write events existed)
# ---------------------------------------------------------------------------


def test_legacy_points_still_warn_first_writes_after_boundary(store_env):
    """Points recorded the pre-events way (no events anywhere) keep
    the accuracy they always had: a file FIRST written after the
    boundary is still warned about via the cumulative difference."""
    sid = session_store.create_session("Legacy points")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(
        sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(
        sid, "task-1", 1, "", "After planner", ["x.txt"])
    rewind_manifest.record_point(
        sid, "task-1", 2, "", "Reply complete", ["x.txt", "y.txt"])

    result = rewind_service.stage_rewind(sid, "task-1", 1, "")
    assert result["ok"] is True
    assert result["files_not_reverted"] == ["y.txt"]
    assert rewind_service.undo_staged(sid)["restored"] is True
