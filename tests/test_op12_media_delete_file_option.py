"""OP12 (Epic 8 PRD Open Point 12): media "also delete the file" option.

Decided reading: deleting a media item removes its library INDEX
entry by default; only an explicit per-delete choice
(``delete_underlying_file: true`` on MEDIA_DELETE_REQUEST) also
unlinks the underlying file from disk.

The engine half already existed (``MediaStore.delete_media(...,
delete_underlying_file=...)`` + ``_unlink_media_file`` from S4-M3,
store-level behaviour pinned in test_media_store.py). These tests pin
the handler/store SEAM end to end — a real MEDIA_DELETE_REQUEST over
the websocket, a real MediaStore on a tmp path, and real files on
disk:

  * flag on  -> file gone + entry gone, response reports both;
  * flag absent (the additive default) -> entry gone, file remains;
  * flag on but the unlink cannot happen -> the entry is still
    deleted and the response honestly reports ``file_deleted: False``
    instead of claiming both are gone;
  * unknown id -> ``deleted: False``.
"""
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.media import store as media_store_mod
from aether_engine.media.store import MediaStore


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    """Isolated engine auth + a tmp-path media store behind the
    handler's lazy ``get_media_store`` import."""
    engine_state.token_path = tmp_path / "engine.token"
    # Mirror the established isolation pattern of every other suite
    # that calls initialize_auth() (test_engine_ws, test_browser_core,
    # test_browser_policy, ...): repoint the global audit logger into
    # this test's tmp dir too. Those suites set log_path per test and
    # delete their tmp dir on teardown WITHOUT restoring it, so in
    # full-suite order the inherited log_path dangles into a removed
    # directory and initialize()'s ENGINE_STARTED write raises
    # FileNotFoundError. Setting both paths here makes this fixture
    # independent of whatever ran before it.
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()

    store = MediaStore(db_path=tmp_path / "media.db")
    store.init_db()
    monkeypatch.setattr(media_store_mod, "get_media_store", lambda: store)
    return SimpleNamespace(store=store, tmp_path=tmp_path)


def _register_file(store, tmp_path, name="photo.png", collection="user"):
    f = tmp_path / name
    f.write_bytes(b"fake-image-bytes")
    item = store.register_media(
        file_path=str(f), collection=collection, media_type="image"
    )
    return f, item


def _delete_over_ws(media_id, payload_extra, request_id="op12-req"):
    """Send one MEDIA_DELETE_REQUEST on a fresh connection and return
    the MEDIA_DELETE_RESPONSE payload."""
    payload = {"media_id": media_id}
    payload.update(payload_extra)
    with TestClient(app) as client:
        token = engine_state.auth_token
        with client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.type == EventType.HELLO
            ws.send_text(
                Event(
                    type=EventType.MEDIA_DELETE_REQUEST,
                    request_id=request_id,
                    payload=payload,
                ).to_json()
            )
            for _ in range(20):
                ev = Event.from_json(ws.receive_text())
                if (
                    ev.type == EventType.MEDIA_DELETE_RESPONSE
                    and ev.request_id == request_id
                ):
                    return ev.payload
    raise AssertionError("no MEDIA_DELETE_RESPONSE received")


def test_flag_on_removes_file_and_entry(media_env):
    f, item = _register_file(media_env.store, media_env.tmp_path)

    payload = _delete_over_ws(item["id"], {"delete_underlying_file": True})

    assert payload["deleted"] is True
    assert payload["file_deleted"] is True
    assert payload["media_id"] == item["id"]
    assert not f.exists()
    assert media_env.store.get_media(item["id"]) is None


def test_flag_absent_defaults_to_index_only(media_env):
    f, item = _register_file(media_env.store, media_env.tmp_path)

    payload = _delete_over_ws(item["id"], {})

    assert payload["deleted"] is True
    assert payload["file_deleted"] is False
    assert f.exists()
    assert media_env.store.get_media(item["id"]) is None


def test_flag_on_for_generated_media_removes_file(media_env):
    f, item = _register_file(
        media_env.store, media_env.tmp_path,
        name="diagram.png", collection="generated",
    )

    payload = _delete_over_ws(item["id"], {"delete_underlying_file": True})

    assert payload["deleted"] is True
    assert payload["file_deleted"] is True
    assert not f.exists()
    assert media_env.store.get_media(item["id"]) is None


def test_unlink_failure_is_surfaced_not_hidden(media_env):
    f, item = _register_file(media_env.store, media_env.tmp_path)
    # Replace the file with a directory at the same path: the path
    # exists but is not a regular file, so the store's unlink helper
    # cannot remove it (deterministic, permission-independent).
    f.unlink()
    f.mkdir()

    payload = _delete_over_ws(item["id"], {"delete_underlying_file": True})

    # The entry deletion stands; the response must NOT claim the file
    # was deleted — file_deleted stays False so the client can tell
    # the user the truth.
    assert payload["deleted"] is True
    assert payload["file_deleted"] is False
    assert media_env.store.get_media(item["id"]) is None
    assert f.is_dir()


def test_unknown_media_id_reports_not_deleted(media_env):
    payload = _delete_over_ws("no-such-id", {"delete_underlying_file": True})

    assert payload["deleted"] is False
    assert payload["file_deleted"] is False
