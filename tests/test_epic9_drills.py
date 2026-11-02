"""Epic 9, E9-05 — reliability drills.

Each failure mode must end in a bounded, user-readable state —
never a hang or a silent death:

- Reconnect storm: 20 rapid connect/disconnect cycles leave the
  engine healthy and answering.
- Locked session DB: a second writer holding the SQLite lock makes
  add_message fail with a readable OperationalError inside the
  sqlite busy-timeout bound (the WS layer logs it and the task
  stream continues), and writes recover once the lock is released.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import aether_engine.routes.websocket as ws_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app
from aether_engine.memory import session_store
from aether_engine.state import engine_state


def test_reconnect_storm_leaves_engine_healthy(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)

    with (
        patch.object(ws_module, "get_pending_approvals", AsyncMock(return_value=[])),
        TestClient(app) as client,
    ):
        for _ in range(20):
            with client.websocket_connect("/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
                hello = Event.from_json(ws.receive_text())
                assert hello.type == EventType.HELLO
        # After the storm, a fresh connection still gets served.
        with client.websocket_connect("/ws/tasks", headers={"Authorization": f"Bearer {engine_state.auth_token}"}) as ws:
            ws.receive_text()
            ws.send_text(Event(type=EventType.PING).to_json())
            pong = Event.from_json(ws.receive_text())
            assert pong.type == EventType.PONG


def test_locked_session_db_fails_bounded_and_recovers(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", db)
    monkeypatch.setattr(session_store, "_initialized", False)
    sid = session_store.create_session("drill")

    locker = sqlite3.connect(db)
    locker.execute("BEGIN EXCLUSIVE")  # holds the write lock
    try:
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            session_store.add_message(sid, "user", "blocked write")
        elapsed = time.monotonic() - start
        assert "locked" in str(excinfo.value).lower()
        # Bounded by sqlite's busy timeout (default 5s) — never a hang.
        assert elapsed < 15
    finally:
        locker.rollback()
        locker.close()

    # Recovery: once the lock is gone, writes succeed again.
    session_store.add_message(sid, "user", "after release")
    sess = session_store.get_session(sid)
    assert sess["messages"][-1]["content"] == "after release"
