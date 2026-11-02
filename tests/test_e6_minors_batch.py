"""Epic 6 minors batch — engine-side seam (E6-F11).

The Projects view renders `is_active` / `exists` badges from the
workspace entries it holds. WORKSPACE_LIST_RESPONSE carries both
fields, but WORKSPACE_ADD_RESPONSE used to carry neither, so a freshly
added workspace rendered with no Active badge and no missing-folder
state until some later LIST refetch repaired it — and removing the
active workspace left its successor unmarked for the same reason.

These tests drive the real websocket handler (the pattern the other
engine_ws suites use; the suite conftest sandboxes the engine home)
and assert the ADD reply carries the same badge fields LIST computes,
for both the existing-folder and the missing-folder shapes.
"""

import json

import pytest
from fastapi.testclient import TestClient

from aether_engine.app import app, engine_state


@pytest.fixture(autouse=True)
def _ws_setup(tmp_path):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    token = engine_state.initialize_auth()
    saved_roots = list(engine_state.user_config.workspace_roots or [])
    engine_state.user_config.workspace_roots = []
    yield token
    engine_state.user_config.workspace_roots = saved_roots


def _connect(token):
    client = TestClient(app)
    return client.websocket_connect(
        "/ws/tasks", headers={"Authorization": f"Bearer {token}"})


def _send(ws, msg_type, payload=None):
    ws.send_text(json.dumps({
        "type": msg_type,
        "schema_version": 1,
        "request_id": "e6-minors-1",
        "payload": payload or {},
    }))


def _recv_until(ws, msg_type):
    for _ in range(30):
        data = json.loads(ws.receive_text())
        if data.get("type") == msg_type:
            return data
    raise AssertionError(f"never received {msg_type}")


def test_workspace_add_reply_carries_active_and_exists(_ws_setup, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        _send(ws, "WORKSPACE_ADD_REQUEST", {"path": str(folder)})
        reply = _recv_until(ws, "WORKSPACE_ADD_RESPONSE")
        assert reply["payload"]["success"] is True
        workspace = reply["payload"]["workspace"]
        # The first (only) root is the active workspace, and the
        # folder exists — the same values LIST reports for it.
        assert workspace["is_active"] is True
        assert workspace["exists"] is True

        _send(ws, "WORKSPACE_LIST_REQUEST")
        listed = _recv_until(ws, "WORKSPACE_LIST_RESPONSE")
        entry = listed["payload"]["workspaces"][0]
        assert workspace["is_active"] == entry["is_active"]
        assert workspace["exists"] == entry["exists"]


def test_workspace_add_reply_marks_missing_folder_and_inactive(_ws_setup, tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    missing = tmp_path / "gone"  # never created
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        _send(ws, "WORKSPACE_ADD_REQUEST", {"path": str(first)})
        _recv_until(ws, "WORKSPACE_ADD_RESPONSE")

        _send(ws, "WORKSPACE_ADD_REQUEST", {"path": str(missing)})
        reply = _recv_until(ws, "WORKSPACE_ADD_RESPONSE")
        assert reply["payload"]["success"] is True
        workspace = reply["payload"]["workspace"]
        # Second root is not active; the folder does not exist.
        assert workspace["is_active"] is False
        assert workspace["exists"] is False
