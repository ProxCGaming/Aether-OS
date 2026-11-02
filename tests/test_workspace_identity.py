"""Per-workspace sessions, slice 2 — workspace identity hygiene.

Owner decisions (grill Q2/Q8): a session binds to the CANONICALISED
workspace root path, and a second spelling of the same folder is
refused with a pointer to the existing entry. Before this slice the
workspace handlers compared raw strings, so ``/proj``, ``/proj/`` and
a symlink to ``/proj`` were three different "workspaces" — and a
config file carrying two spellings loaded both.

These tests pin the identity rule at its three seams:

* the canonicalisation helpers in ``aether_engine.config``,
* config load (``UserConfig.from_dict``) collapsing pre-existing
  duplicate spellings to the first occurrence's canonical form,
* the WORKSPACE_ADD / SET_ACTIVE / REMOVE socket handlers acting on
  canonical identity (ADD refusal carries the existing entry).
"""

import json

import pytest
from fastapi.testclient import TestClient

from aether_engine.app import app, engine_state
from aether_engine.config import (
    UserConfig,
    canonical_workspace_root,
    find_workspace_root,
    remove_workspace_root,
    workspace_root_key,
)

# ---------------------------------------------------------------------------
# Canonicalisation units
# ---------------------------------------------------------------------------

def test_canonical_strips_trailing_separator(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    assert canonical_workspace_root(str(folder) + "/") == str(folder.resolve())
    assert canonical_workspace_root(str(folder)) == str(folder.resolve())


def test_canonical_collapses_dot_segments(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    dotted = str(tmp_path / "elsewhere" / ".." / "proj" / "." / "")
    assert canonical_workspace_root(dotted) == str(folder.resolve())


def test_canonical_relative_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert canonical_workspace_root("rel") == str((tmp_path / "rel").resolve())


def test_canonical_resolves_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert canonical_workspace_root(str(link)) == str(real.resolve())
    assert canonical_workspace_root(str(link)) == canonical_workspace_root(str(real))


def test_canonical_missing_folder_still_canonicalises(tmp_path):
    missing = tmp_path / "gone" / ".." / "gone"
    # A missing-folder root is a deliberate Projects state; resolving
    # it must not raise and must produce the absolute canonical form.
    assert canonical_workspace_root(str(missing)) == str((tmp_path / "gone").resolve())


def test_canonical_is_total_on_odd_input():
    assert canonical_workspace_root("") == ""
    assert canonical_workspace_root("   ") == "   "
    assert canonical_workspace_root(None) is None  # type: ignore[arg-type]


def test_root_key_windows_comparison_folds_case():
    # On Windows folder identity is case-insensitive: two spellings
    # differing only in case are the same workspace. POSIX keeps them
    # distinct, exactly as its filesystems do.
    assert workspace_root_key("/Tmp/Proj", platform="win32") == \
        workspace_root_key("/tmp/proj", platform="win32")
    assert workspace_root_key("/Tmp/Proj", platform="posix") != \
        workspace_root_key("/tmp/proj", platform="posix")


def test_find_and_remove_by_canonical_identity(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    roots = [str(folder.resolve()), str(tmp_path / "other")]
    assert find_workspace_root(roots, str(folder) + "/") == str(folder.resolve())
    assert find_workspace_root(roots, str(tmp_path / "nope")) is None
    removed = remove_workspace_root(roots, str(folder) + "/")
    assert removed == str(folder.resolve())
    assert roots == [str(tmp_path / "other")]
    assert remove_workspace_root(roots, str(folder)) is None


# ---------------------------------------------------------------------------
# Config load: pre-existing duplicate spellings collapse
# ---------------------------------------------------------------------------

def test_from_dict_dedupes_duplicate_spellings(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    cfg = UserConfig.from_dict({"workspace_roots": [
        str(folder) + "/",            # first spelling wins
        str(other),
        str(folder),                   # same folder, plainer spelling
        str(tmp_path / "x" / ".." / "proj"),  # same folder, dotted
    ]})
    assert cfg.workspace_roots == [str(folder.resolve()), str(other.resolve())]


def test_from_dict_keeps_missing_folder_roots(tmp_path):
    missing = tmp_path / "gone"
    cfg = UserConfig.from_dict({"workspace_roots": [str(missing)]})
    assert cfg.workspace_roots == [str(missing.resolve())]


def test_from_dict_workspace_roots_stays_total():
    assert UserConfig.from_dict({}).workspace_roots == []
    assert UserConfig.from_dict({"workspace_roots": "junk"}).workspace_roots == []
    # Non-string debris passes through untouched — load never raises.
    cfg = UserConfig.from_dict({"workspace_roots": [123, "/x"]})
    assert cfg.workspace_roots[0] == 123
    assert cfg.workspace_roots[1] == canonical_workspace_root("/x")


def test_config_round_trip_is_stable(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    cfg = UserConfig.from_dict({"workspace_roots": [str(folder) + "/"]})
    again = UserConfig.from_dict(cfg.to_dict())
    assert again.workspace_roots == cfg.workspace_roots


# ---------------------------------------------------------------------------
# Socket handlers act on canonical identity
# ---------------------------------------------------------------------------

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
        "request_id": "ws-sessions-s2",
        "payload": payload or {},
    }))


def _recv_until(ws, msg_type):
    for _ in range(30):
        data = json.loads(ws.receive_text())
        if data.get("type") == msg_type:
            return data
    raise AssertionError(f"never received {msg_type}")


def _add(ws, path):
    _send(ws, "WORKSPACE_ADD_REQUEST", {"path": path})
    return _recv_until(ws, "WORKSPACE_ADD_RESPONSE")


def test_add_stores_canonical_form(_ws_setup, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        reply = _add(ws, str(folder) + "/")
        assert reply["payload"]["success"] is True
        assert reply["payload"]["workspace"]["path"] == str(folder.resolve())
        assert engine_state.user_config.workspace_roots == [str(folder.resolve())]


def test_add_refuses_duplicate_spelling_with_pointer(_ws_setup, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        assert _add(ws, str(folder))["payload"]["success"] is True

        reply = _add(ws, str(folder) + "/")
        payload = reply["payload"]
        assert payload["success"] is False
        assert "already exists" in payload["error"]
        # The refusal points at the existing entry, in the same badge
        # shape a LIST row carries, so the UI can highlight it.
        assert payload["exists"] is True
        existing = payload["workspace"]
        assert existing["path"] == str(folder.resolve())
        assert existing["is_active"] is True
        assert existing["exists"] is True
        # Nothing was added: still exactly one workspace.
        assert engine_state.user_config.workspace_roots == [str(folder.resolve())]


def test_add_refuses_symlink_spelling(_ws_setup, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        assert _add(ws, str(real))["payload"]["success"] is True
        reply = _add(ws, str(link))
        assert reply["payload"]["success"] is False
        assert reply["payload"]["workspace"]["path"] == str(real.resolve())
        assert len(engine_state.user_config.workspace_roots) == 1


def test_add_empty_path_still_refused(_ws_setup):
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        reply = _add(ws, "")
        assert reply["payload"]["success"] is False
        assert engine_state.user_config.workspace_roots == []


def test_set_active_by_variant_spelling(_ws_setup, tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        assert _add(ws, str(first))["payload"]["success"] is True
        assert _add(ws, str(second))["payload"]["success"] is True

        _send(ws, "WORKSPACE_SET_ACTIVE_REQUEST", {"path": str(first) + "/"})
        reply = _recv_until(ws, "WORKSPACE_SET_ACTIVE_RESPONSE")
        assert reply["payload"]["success"] is True
        assert reply["payload"]["path"] == str(first.resolve())
        roots = engine_state.user_config.workspace_roots
        assert roots[0] == str(first.resolve())
        assert len(roots) == 2


def test_remove_by_variant_spelling_reports_canonical(_ws_setup, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        assert _add(ws, str(folder))["payload"]["success"] is True

        _send(ws, "WORKSPACE_REMOVE_REQUEST", {"path": str(folder) + "/"})
        reply = _recv_until(ws, "WORKSPACE_REMOVE_RESPONSE")
        assert reply["payload"]["success"] is True
        # The reply names the canonical root that was actually
        # removed — slice 3 unbinds sessions by this value.
        assert reply["payload"]["path"] == str(folder.resolve())
        assert engine_state.user_config.workspace_roots == []


def test_remove_unknown_still_not_found(_ws_setup, tmp_path):
    with _connect(_ws_setup) as ws:
        _recv_until(ws, "HELLO")
        _send(ws, "WORKSPACE_REMOVE_REQUEST", {"path": str(tmp_path / "nope")})
        reply = _recv_until(ws, "WORKSPACE_REMOVE_RESPONSE")
        assert reply["payload"]["success"] is False
