"""Per-workspace sessions, slice 3 — WS contracts + handlers.

Owner decisions (grill Q3/Q4/Q5/Q6/Q7/Q10): a session binds at
creation to the then-active workspace (or to "No workspace" — a
permanent, first-class home); the binding is the canonicalised
root path; a move re-files it (organisational only, Q11); removing
a workspace unbinds its sessions and never deletes them; search
results carry the binding as a label.

These tests pin the socket surface only: SESSION_CREATE's three
workspace-field states, SESSION_LIST's three filter states,
SESSION_MOVE (the new pair), WORKSPACE_LIST session counts,
WORKSPACE_REMOVE unbinding, SESSION_SEARCH label passthrough, and
the task pump's implicit first-message creation taking the same
binding rule. Store semantics are slice 1's tests; canonical
identity is slice 2's.
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import canonical_workspace_root
from aether_engine.memory import session_store
from aether_engine.providers.base import StreamChunk
from aether_engine.routes import websocket as ws_module


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + session store isolated to tmp (the fast-chat /
    engine-ws house pattern). The app lifespan re-runs initialize()
    at TestClient startup, so config seeding happens on the live
    user_config object inside each test, after client entry."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    saved_roots = list(engine_state.user_config.workspace_roots or [])
    yield {"tmp_path": tmp_path}
    engine_state.user_config.workspace_roots = saved_roots


@contextmanager
def _socket():
    with TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        yield ws


def _seed_roots(*roots):
    engine_state.user_config.workspace_roots = [
        canonical_workspace_root(r) for r in roots]


def _send(ws, msg_type, payload=None):
    ws.send_text(Event(type=msg_type, payload=payload or {}).to_json())


def _recv_until(ws, msg_type, max_events=60):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == msg_type:
            return ev
    raise AssertionError(f"never received {msg_type}")


def _create(ws, payload):
    _send(ws, EventType.SESSION_CREATE_REQUEST, payload)
    return _recv_until(ws, EventType.SESSION_CREATE_RESPONSE).payload


def _stored(session_id):
    return session_store.get_session(session_id)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def test_move_contract_pair_registered():
    assert EventType.SESSION_MOVE_REQUEST.value == "SESSION_MOVE_REQUEST"
    assert EventType.SESSION_MOVE_RESPONSE.value == "SESSION_MOVE_RESPONSE"


# ---------------------------------------------------------------------------
# SESSION_CREATE: the binding rule's three field states
# ---------------------------------------------------------------------------

def test_create_absent_workspace_binds_active_root(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    with _socket() as ws:
        _seed_roots(str(alpha), str(beta))
        payload = _create(ws, {"title": "First"})
    assert payload["workspace"] == str(alpha.resolve())
    assert _stored(payload["session_id"])["workspace"] == str(alpha.resolve())


def test_create_absent_workspace_with_no_roots_is_unbound(ws_env):
    with _socket() as ws:
        _seed_roots()
        payload = _create(ws, {"title": "Homeless"})
    assert payload["workspace"] is None
    assert _stored(payload["session_id"])["workspace"] is None


def test_create_explicit_workspace_binds_it_by_canonical_identity(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    with _socket() as ws:
        _seed_roots(str(alpha), str(beta))
        # A variant spelling of beta binds beta's canonical root.
        payload = _create(ws, {"title": "For beta", "workspace": str(beta) + "/"})
    assert payload["workspace"] == str(beta.resolve())
    assert _stored(payload["session_id"])["workspace"] == str(beta.resolve())


def test_create_explicit_null_is_unbound(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    with _socket() as ws:
        _seed_roots(str(alpha))
        payload = _create(ws, {"title": "Deliberately homeless", "workspace": None})
    assert payload["workspace"] is None
    assert _stored(payload["session_id"])["workspace"] is None


def test_create_unconfigured_workspace_fails_honestly(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    with _socket() as ws:
        _seed_roots(str(alpha))
        payload = _create(ws, {"title": "Nope", "workspace": str(stranger)})
    assert payload["session_id"] is None
    assert payload["success"] is False
    assert "not a configured" in payload["error"]
    # Nothing was filed under a workspace the app does not know.
    assert session_store.list_sessions() == []


# ---------------------------------------------------------------------------
# SESSION_LIST: three filter states; every row carries its binding
# ---------------------------------------------------------------------------

def test_list_filter_three_states(ws_env, tmp_path):
    alpha = str((tmp_path / "alpha").resolve())
    beta = str((tmp_path / "beta").resolve())
    a_id = session_store.create_session("Alpha chat", workspace=alpha)
    b_id = session_store.create_session("Beta chat", workspace=beta)
    u_id = session_store.create_session("Homeless chat")

    with _socket() as ws:
        _send(ws, EventType.SESSION_LIST_REQUEST, {})
        all_rows = _recv_until(ws, EventType.SESSION_LIST_RESPONSE).payload["sessions"]
        _send(ws, EventType.SESSION_LIST_REQUEST, {"workspace": alpha + "/"})
        alpha_rows = _recv_until(ws, EventType.SESSION_LIST_RESPONSE).payload["sessions"]
        _send(ws, EventType.SESSION_LIST_REQUEST, {"workspace": None})
        unbound_rows = _recv_until(ws, EventType.SESSION_LIST_RESPONSE).payload["sessions"]

    by_id = {row["id"]: row for row in all_rows}
    assert set(by_id) == {a_id, b_id, u_id}
    assert by_id[a_id]["workspace"] == alpha
    assert by_id[b_id]["workspace"] == beta
    assert by_id[u_id]["workspace"] is None
    assert [row["id"] for row in alpha_rows] == [a_id]
    assert [row["id"] for row in unbound_rows] == [u_id]


# ---------------------------------------------------------------------------
# SESSION_MOVE
# ---------------------------------------------------------------------------

def test_move_to_workspace_then_unbound(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    sess_id = session_store.create_session("Mover")
    with _socket() as ws:
        _seed_roots(str(alpha))
        _send(ws, EventType.SESSION_MOVE_REQUEST,
              {"session_id": sess_id, "workspace": str(alpha) + "/"})
        moved = _recv_until(ws, EventType.SESSION_MOVE_RESPONSE).payload
        assert moved["moved"] is True
        assert moved["session_id"] == sess_id
        assert moved["workspace"] == str(alpha.resolve())
        assert _stored(sess_id)["workspace"] == str(alpha.resolve())

        _send(ws, EventType.SESSION_MOVE_REQUEST,
              {"session_id": sess_id, "workspace": None})
        unbound = _recv_until(ws, EventType.SESSION_MOVE_RESPONSE).payload
        assert unbound["moved"] is True
        assert unbound["workspace"] is None
        assert _stored(sess_id)["workspace"] is None


def test_move_unknown_session_fails(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    with _socket() as ws:
        _seed_roots(str(alpha))
        _send(ws, EventType.SESSION_MOVE_REQUEST,
              {"session_id": "no-such-session", "workspace": str(alpha)})
        payload = _recv_until(ws, EventType.SESSION_MOVE_RESPONSE).payload
    assert payload["moved"] is False
    assert "error" in payload


def test_move_to_unconfigured_workspace_fails_and_keeps_binding(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    sess_id = session_store.create_session(
        "Settled", workspace=str(alpha.resolve()))
    with _socket() as ws:
        _seed_roots(str(alpha))
        _send(ws, EventType.SESSION_MOVE_REQUEST,
              {"session_id": sess_id, "workspace": str(stranger)})
        payload = _recv_until(ws, EventType.SESSION_MOVE_RESPONSE).payload
    assert payload["moved"] is False
    assert "not a configured" in payload["error"]
    # The reply reports the binding as it stands — unmoved.
    assert payload["workspace"] == str(alpha.resolve())
    assert _stored(sess_id)["workspace"] == str(alpha.resolve())


# ---------------------------------------------------------------------------
# WORKSPACE_LIST counts + WORKSPACE_REMOVE unbinding
# ---------------------------------------------------------------------------

def test_workspace_list_carries_session_counts(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    session_store.create_session("A1", workspace=str(alpha.resolve()))
    session_store.create_session("A2", workspace=str(alpha.resolve()))
    session_store.create_session("B1", workspace=str(beta.resolve()))
    session_store.create_session("U1")
    session_store.create_session("U2")
    session_store.create_session("U3")
    with _socket() as ws:
        _seed_roots(str(alpha), str(beta))
        _send(ws, EventType.WORKSPACE_LIST_REQUEST, {})
        payload = _recv_until(ws, EventType.WORKSPACE_LIST_RESPONSE).payload
    rows = {row["path"]: row for row in payload["workspaces"]}
    assert rows[str(alpha.resolve())]["session_count"] == 2
    assert rows[str(beta.resolve())]["session_count"] == 1
    assert payload["unbound_session_count"] == 3


def test_workspace_remove_unbinds_its_sessions(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    beta = tmp_path / "beta"
    beta.mkdir()
    a1 = session_store.create_session("A1", workspace=str(alpha.resolve()))
    a2 = session_store.create_session("A2", workspace=str(alpha.resolve()))
    b1 = session_store.create_session("B1", workspace=str(beta.resolve()))
    with _socket() as ws:
        _seed_roots(str(alpha), str(beta))
        # Address the removal by a variant spelling (canonical identity).
        _send(ws, EventType.WORKSPACE_REMOVE_REQUEST, {"path": str(alpha) + "/"})
        payload = _recv_until(ws, EventType.WORKSPACE_REMOVE_RESPONSE).payload
    assert payload["success"] is True
    assert payload["unbound_sessions"] == 2
    # Removal unbinds — it never deletes (Q7).
    assert _stored(a1)["workspace"] is None
    assert _stored(a2)["workspace"] is None
    assert _stored(b1)["workspace"] == str(beta.resolve())
    assert engine_state.user_config.workspace_roots == [str(beta.resolve())]


# ---------------------------------------------------------------------------
# SESSION_SEARCH: the binding rides along as a label (Q10)
# ---------------------------------------------------------------------------

def test_search_results_carry_workspace_label(ws_env, tmp_path):
    alpha = str((tmp_path / "alpha").resolve())
    session_store.create_session("Quixotic roadmap", workspace=alpha)
    session_store.create_session("Quixotic loose note")
    with _socket() as ws:
        _send(ws, EventType.SESSION_SEARCH_REQUEST, {"query": "Quixotic"})
        payload = _recv_until(ws, EventType.SESSION_SEARCH_RESPONSE).payload
    by_title = {row["title"]: row for row in payload["results"]}
    assert by_title["Quixotic roadmap"]["workspace"] == alpha
    assert by_title["Quixotic loose note"]["workspace"] is None


# ---------------------------------------------------------------------------
# Fork: the empty step-fork branch inherits too (slice 3 fix)
# ---------------------------------------------------------------------------

def test_empty_step_fork_inherits_binding(ws_env):
    """fork_session_at's step-0-of-the-only-task branch creates the
    fork directly instead of via the store's fork_session — it must
    pass the source's binding explicitly, or that one fork shape
    would silently land unbound while every other fork inherits."""
    from aether_engine.rewind import fork as fork_service
    from aether_engine.rewind import manifest as rewind_manifest

    alpha = "/projects/alpha"  # binding strings are opaque to the store
    sid = session_store.create_session("Bound source", workspace=alpha)
    session_store.add_message(sid, "user", "the only turn")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    result = fork_service.fork_session_at(sid, step_index=0)
    assert result["ok"] is True
    assert result["messages_copied"] == 0
    assert result["workspace"] == alpha
    assert session_store.get_session(result["new_session_id"])["workspace"] == alpha


# ---------------------------------------------------------------------------
# Task pump: an implicit first-message session takes the same rule
# ---------------------------------------------------------------------------

class _ChatProvider:
    """Instant CHAT verdict; the chat stream yields fixed chunks
    (the fast-chat test pattern)."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append(node_name)
        if node_name == "intent_classifier":
            yield StreamChunk(text="CHAT", finish_reason="stop")
            return
        yield StreamChunk(text="Hi there!")
        yield StreamChunk(finish_reason="stop")


def _task_stack(provider):
    stack = ExitStack()
    mock_store = stack.enter_context(
        patch.object(ws_module, "SecretStore")).return_value
    mock_store.list_providers.return_value = ["google_gemini"]
    stack.enter_context(patch.object(
        ws_module, "_create_provider_instance", return_value=provider))
    graph_calls = []
    stack.enter_context(patch.object(
        ws_module, "run_langgraph_task",
        lambda *a, **k: graph_calls.append(k) or _empty_graph()))
    return stack


async def _empty_graph():
    yield Event(type=EventType.TASK_COMPLETED, payload={"state": "succeeded"})


def test_first_message_session_binds_active_root(ws_env, tmp_path):
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    provider = _ChatProvider()
    with _task_stack(provider), _socket() as ws:
        _seed_roots(str(alpha))
        _send(ws, EventType.START_TASK,
              {"prompt": "Hello, how are you today?", "session_id": None})
        created = _recv_until(ws, EventType.SESSION_CREATE_RESPONSE).payload
        _recv_until(ws, EventType.TASK_COMPLETED)
    assert created["workspace"] == str(alpha.resolve())
    assert _stored(created["session_id"])["workspace"] == str(alpha.resolve())


def test_first_message_session_unbound_without_roots(ws_env):
    provider = _ChatProvider()
    with _task_stack(provider), _socket() as ws:
        _seed_roots()
        _send(ws, EventType.START_TASK,
              {"prompt": "Hello, how are you today?", "session_id": None})
        created = _recv_until(ws, EventType.SESSION_CREATE_RESPONSE).payload
        _recv_until(ws, EventType.TASK_COMPLETED)
    assert created["workspace"] is None
    assert _stored(created["session_id"])["workspace"] is None
