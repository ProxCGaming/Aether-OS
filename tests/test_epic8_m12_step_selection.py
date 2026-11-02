"""Epic 8, M12: exact step selection for rewind/fork.

The audit found the desktop could not select the step the user
picked: it mapped messages to the FIRST or FINAL manifest point,
the step menus vanished entirely on sessions with no manifest
(forked / imported histories — "fork anywhere" was dead there),
and after a rewind the desktop reconstructed the retained history
locally, disagreeing with the engine at the final point (the
engine discards no messages there; the desktop removed the
response anyway).

The engine-side contracts this slice pins:

  * REWIND_MANIFEST_GET entries carry ``cut_message_id`` — the
    message the step's retained history ends at, computed from the
    session's authoritative record with the same anchoring the
    rewind/fork services apply (the task begins at the session's
    last user row).
  * SESSION_FORK responses carry the fork's retained ``messages``
    exactly as stored, so what the user sees is what the engine
    kept — including forks at a message boundary in a session with
    no manifest at all.
  * TASK_REWIND responses carry the session's retained ``messages``
    after staging — at the final point that is the FULL history.
"""
import pytest

from aether_engine.memory import session_store
from aether_engine.rewind import fork as fork_service
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest + rewind backups,
    which share its DB) isolated to a tmp memory.db."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


def _contents(session_id):
    sess = session_store.get_session(session_id)
    return [(m["role"], m["content"]) for m in sess["messages"]]


# ---------------------------------------------------------------------------
# Manifest boundaries: every entry names the message its history ends at
# ---------------------------------------------------------------------------


def test_manifest_entries_carry_their_message_boundary(store_env):
    sid = session_store.create_session("Two tasks")
    session_store.add_message(sid, "user", "first task")
    a1 = session_store.add_message(sid, "assistant", "first answer")
    u2 = session_store.add_message(sid, "user", "second task")
    a2 = session_store.add_message(sid, "assistant", "second answer")
    # The last task's recorded points (fast-chat style: no graph
    # checkpoints, honest "" ids).
    rewind_manifest.record_point(sid, "task-2", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-2", 1, "", "After a step", [])
    rewind_manifest.record_point(sid, "task-2", 2, "", "Reply complete", [])

    payload = rewind_manifest.get_manifest(sid)
    cuts = [e["cut_message_id"] for e in payload["entries"]]
    # Step 0 ends before the task began (at the previous task's last
    # message); an intermediate point ends at the task's prompt; the
    # final point ends at the session's last message.
    assert cuts == [a1, u2, a2]


def test_manifest_step_zero_boundary_is_none_for_a_lone_task(store_env):
    sid = session_store.create_session("Only task")
    session_store.add_message(sid, "user", "task")
    a1 = session_store.add_message(sid, "assistant", "answer")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    payload = rewind_manifest.get_manifest(sid)
    cuts = [e["cut_message_id"] for e in payload["entries"]]
    # Nothing precedes the only task: step 0 retains no message.
    assert cuts == [None, a1]


# ---------------------------------------------------------------------------
# Forks without a manifest: retained history is the engine's own record
# ---------------------------------------------------------------------------


def test_fork_at_message_boundary_without_any_manifest(store_env):
    """An imported / inherited history has no manifest rows at all.
    Forking at a message boundary must still work, and the response
    must carry the fork's retained history exactly as the engine
    stored it (fresh ids, same roles/contents/order)."""
    sid = session_store.create_session("Imported")
    session_store.add_message(sid, "user", "one")
    a1 = session_store.add_message(sid, "assistant", "first")
    session_store.add_message(sid, "user", "two")
    session_store.add_message(sid, "assistant", "second")
    assert rewind_manifest.get_manifest(sid)["entries"] == []

    result = fork_service.fork_session_at(sid, message_id=a1)
    assert result["ok"] is True
    fork_id = result["new_session_id"]

    stored = session_store.get_session(fork_id)["messages"]
    assert [(m["role"], m["content"]) for m in result["messages"]] == [
        ("user", "one"), ("assistant", "first")]
    # The returned history IS the stored history, id for id.
    assert [m["id"] for m in result["messages"]] == [m["id"] for m in stored]
    assert result["messages_copied"] == 2
    # The source is untouched.
    assert len(_contents(sid)) == 4


def test_fork_of_a_fork_at_a_message_boundary(store_env):
    """A fork records no manifest for its inherited history, so a
    second-generation fork is again a manifest-free message
    boundary fork — and must still land on the exact message."""
    sid = session_store.create_session("Original")
    u1 = session_store.add_message(sid, "user", "one")
    session_store.add_message(sid, "assistant", "first")
    session_store.add_message(sid, "user", "two")

    first = fork_service.fork_session_at(sid, message_id=u1)
    assert first["ok"] is True
    fork_id = first["new_session_id"]
    assert rewind_manifest.get_manifest(fork_id)["entries"] == []

    # Fork the fork at its only message (the inherited prompt).
    inherited_id = session_store.get_session(fork_id)["messages"][0]["id"]
    second = fork_service.fork_session_at(fork_id, message_id=inherited_id)
    assert second["ok"] is True
    assert [(m["role"], m["content"]) for m in second["messages"]] == [
        ("user", "one")]
    assert _contents(second["new_session_id"]) == [("user", "one")]


# ---------------------------------------------------------------------------
# Rewind responses: the retained history is the engine's, not a guess
# ---------------------------------------------------------------------------


def _fast_chat_task(store_env, sid, task_id="fc-1"):
    session_store.add_message(sid, "user", "hi")
    session_store.add_message(sid, "assistant", "hello")
    rewind_manifest.record_point(sid, task_id, 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, task_id, 1, "", "Reply complete", [])


def test_rewind_to_final_point_reports_the_full_retained_history(store_env):
    """The final point discards NO stored messages. The response
    must say so: its retained history is the whole conversation,
    response included — the desktop must not remove it locally."""
    sid = session_store.create_session("Fast chat")
    _fast_chat_task(store_env, sid)

    result = rewind_service.stage_rewind(sid, "fc-1", 1, "")
    assert result["ok"] is True
    assert [(m["role"], m["content"]) for m in result["messages"]] == [
        ("user", "hi"), ("assistant", "hello")]
    assert _contents(sid) == [("user", "hi"), ("assistant", "hello")]


def test_rewind_to_step_zero_reports_the_prefix_it_kept(store_env):
    sid = session_store.create_session("Two tasks")
    session_store.add_message(sid, "user", "first task")
    session_store.add_message(sid, "assistant", "first answer")
    _fast_chat_task(store_env, sid, task_id="fc-2")

    result = rewind_service.stage_rewind(sid, "fc-2", 0, "")
    assert result["ok"] is True
    assert [(m["role"], m["content"]) for m in result["messages"]] == [
        ("user", "first task"), ("assistant", "first answer")]
    assert _contents(sid) == [
        ("user", "first task"), ("assistant", "first answer")]
