"""E4-04: ``stage_rewind`` must honour its "they never raise" contract.

The docstring promises: "Expected failures are refusals (``ok``
False with an error), raised before any state changes; they never
raise." Three verified raise paths broke that promise:

1. ``reconcile_pending()`` is called unguarded — any failure inside
   it (e.g. a locked checkpoints.db) propagates out.
2. ``_record_staging_intent`` does a plain INSERT into
   ``rewind_pending`` whose ``session_id`` is the PRIMARY KEY; the
   async wrapper runs staging via ``asyncio.to_thread``, so two
   concurrent stagings for one session can both pass the
   ``has_staged`` check and the loser raises ``IntegrityError``.
3. The staging calls after the intent (``_stage_memory_side``,
   ``_stage_checkpoint_side``, ``_set_pending_phase``) are unguarded,
   so a mid-staging failure propagates the same way — and leaves the
   staging intent behind to wedge later rewinds.

The fix guards each: a reconcile failure refuses, an intent
collision refuses with the same "already waiting" message as the
``has_staged`` refusal, and a mid-staging failure refuses after
dropping its own still-``staging`` intent (a row that reached the
``memory`` phase carries real staged state and is left for
``reconcile_pending``, whose design completes it).
"""
import pytest

from aether_engine.memory import session_store
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest + rewind tables,
    which share its DB) isolated to a tmp memory.db — the rewind
    execution suite's recipe."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


def _seed_session(sid_task="task-1"):
    sid = session_store.create_session("Stageable")
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, sid_task, 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, sid_task, 1, "", "Reply complete", [])
    return sid


def _messages(sid):
    return [(m["role"], m["content"])
            for m in session_store.get_session(sid)["messages"]]


def test_reconcile_failure_is_a_refusal_not_a_raise(store_env, monkeypatch):
    sid = _seed_session()

    def broken_reconcile(*args, **kwargs):
        raise RuntimeError("checkpoints.db is locked")

    monkeypatch.setattr(rewind_service, "reconcile_pending", broken_reconcile)

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")

    assert result["ok"] is False
    assert result["error"]
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]


def test_staging_intent_collision_is_a_refusal_not_a_raise(store_env, monkeypatch):
    sid = _seed_session()
    # A competing staging lands its intent row first (the concurrent
    # staging won the race)…
    rewind_service.init_rewind_service_db()
    with rewind_service._get_conn() as conn:
        conn.execute(
            "INSERT INTO rewind_pending "
            "(session_id, rewind_id, task_id, step_index, checkpoint_id, "
            "expires_at, phase, checkpoint_plan) "
            "VALUES (?, 'competitor', 'task-1', 0, '', 9999999999, 'complete', '[]')",
            (sid,))
        conn.commit()
    # …but this staging's has_staged check ran before the row landed,
    # so the collision surfaces at the intent INSERT itself.
    monkeypatch.setattr(rewind_service, "has_staged", lambda session_id: False)

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")

    assert result["ok"] is False
    assert "already waiting" in result["error"]
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]


def test_mid_staging_failure_refuses_and_leaves_no_wedged_intent(
        store_env, monkeypatch):
    sid = _seed_session()

    real_stage = rewind_service._stage_memory_side

    def broken_stage(*args, **kwargs):
        raise RuntimeError("memory staging exploded")

    monkeypatch.setattr(rewind_service, "_stage_memory_side", broken_stage)

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")

    assert result["ok"] is False
    assert result["error"]
    # The failed staging's intent is gone (it committed nothing), the
    # transcript is untouched…
    assert rewind_service.has_staged(sid) is False
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]
    # …and the session is not wedged: a healthy staging works and
    # undoes cleanly afterwards.
    monkeypatch.setattr(rewind_service, "_stage_memory_side", real_stage)
    staged = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert staged["ok"] is True
    assert rewind_service.undo_staged(sid)["restored"] is True
    assert _messages(sid) == [("user", "hi"), ("assistant", "hello")]
