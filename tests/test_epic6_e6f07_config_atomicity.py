"""E6-F07 — a failed Settings save can destroy the entire previous
UserConfig file.

Three seams, one cause family:

  * ``save_config`` wrote DIRECTLY to the live path — an interrupted
    write (ENOSPC after a prefix flushed) left a truncated invalid
    file where the user's settings used to be.
  * The CONFIG_UPDATE / CONFIG_RESET handlers mutated live state
    BEFORE saving, never handled a storage exception and never rolled
    back — on failure the client got ZERO replies (the socket just
    died), live state kept the unsaved values, and the file on disk
    was the truncated fragment.
  * ``load_config`` silently treated invalid JSON as all-fresh
    defaults, so the destruction was invisible: the next start simply
    "forgot" every setting.

The fixed behaviour pinned here:

  * ``save_config`` is atomic (temp file in the same directory +
    flush/fsync + ``os.replace``, the E3-F04 house pattern): an
    interrupted save leaves the previous file byte-identical and no
    temp file behind.
  * UPDATE/RESET snapshot live state before mutating; on a save
    failure they roll back and ALWAYS reply ``{success: False,
    error}`` — the vocabulary the F05 validation refusal established.
  * ``load_config`` on an unparseable file quarantines it under
    ``config.json.corrupt-<UTC timestamp>``, logs the failure loudly,
    and only then falls back to defaults for the session. A merely
    UNREADABLE file (permissions) is different: it is never
    quarantined or overwritten — defaults for the session, file
    untouched.

The failure injection patches ``pathlib.Path.open`` so writes to the
config file (or its ``.tmp`` sibling) flush a 64-byte prefix to the
real file and then raise ENOSPC — the audit's interrupted save,
synthetic, never a filled disk.
"""
import errno
import json
import logging
import queue
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore

CONFIG_NAMES = {"config.json", "config.json.tmp"}


class DummyProtector(BaseProtector):
    """The test_secret_store.py seam: reversible, no OS keyring."""

    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


class _DyingStream:
    """A text stream whose first write flushes a 64-byte prefix to
    the real file and then raises ENOSPC — an interrupted save."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        self._real.write(data[:64])
        self._real.flush()
        raise OSError(errno.ENOSPC, "No space left on device (injected)")

    def writelines(self, lines):
        self.write("".join(lines))

    def flush(self):
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._real.close()
        return False


def _arm_partial_write(monkeypatch, switch):
    """Patch Path.open so writes to the config file / its temp
    sibling die mid-stream while ``switch["armed"]`` is true."""
    real_open = Path.open

    def dying_open(self, mode="r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if (
            switch["armed"]
            and self.name in CONFIG_NAMES
            and any(c in mode for c in "wxa")
        ):
            return _DyingStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", dying_open)


def _registry() -> ModelRegistry:
    return ModelRegistry([
        ModelEntry(provider="openai", id="gpt-4o-mini", label="GPT-4o mini",
                   capabilities=["chat"], priority=1),
        ModelEntry(provider="google_gemini", id="gemini-3.5-flash",
                   label="Gemini 3.5 Flash", capabilities=["chat"], priority=1),
    ])


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Isolated engine state initialised from a distinctive config.

    Audit log path is set BEFORE initialize_auth() per the fixture
    law; the config lives in a tmp CONFIG_PATH file because
    initialize() reloads user_config from disk.
    """
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=DummyProtector())
    store.save_provider("openai", "sk-test-openai")
    store.save_provider("google_gemini", "sk-test-gemini")
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    original = UserConfig(
        default_provider="openai",
        default_model="gpt-4o-mini",
        disable_fallbacks=True,
        reasoning_effort="high",
    )
    save_config(original, config_path)

    monkeypatch.setattr(engine_state, "model_registry", _registry())
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    return {
        "tmp_path": tmp_path,
        "config_path": config_path,
        "original": original,
        "store": store,
    }


def _recv_until(ws, event_type, max_events=12):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == event_type:
            return ev
    raise AssertionError(f"no {event_type} received")


def _recv_until_timeout(ws, event_type, timeout=30.0):
    """``_recv_until`` with a deadline — for the failure-reply
    receives. Pre-fix, a handler that dies on the save sends NOTHING
    and the connection stays open awaiting more input, so a plain
    receive deadlocks the test exactly the way the real client hung
    (verified: the app loop sits idle in select, the test thread in
    the receive). Receive on a daemon thread and fail on the
    deadline instead of hanging the suite."""
    results: queue.Queue = queue.Queue()

    def _pump():
        try:
            results.put(("event", _recv_until(ws, event_type)))
        except Exception as exc:  # noqa: BLE001 — a pump thread must ferry ANY receive failure to the asserting thread, never die silently.
            results.put(("error", exc))

    threading.Thread(target=_pump, daemon=True).start()
    try:
        kind, value = results.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"no {event_type} within {timeout}s — "
            "the handler sent no reply") from None
    if kind == "error":
        raise value
    return value


def _connect():
    return TestClient(app)


def _ws_connect(client):
    return client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    )


# ---------------------------------------------------------------------------
# save_config atomicity
# ---------------------------------------------------------------------------

class TestSaveConfigAtomicity:
    def test_interrupted_save_preserves_previous_file_byte_identical(
            self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        before_cfg = UserConfig(default_provider="openai",
                                default_model="gpt-4o-mini",
                                disable_fallbacks=True)
        save_config(before_cfg, path)
        before_bytes = path.read_bytes()
        assert len(before_bytes) > 64  # the injected prefix is a prefix

        switch = {"armed": True}
        _arm_partial_write(monkeypatch, switch)
        with pytest.raises(OSError):
            save_config(UserConfig(default_provider="google_gemini"), path)

        # The previous file is untouched, still loads as itself, and
        # the interrupted temp write left nothing behind.
        assert path.read_bytes() == before_bytes
        assert load_config(path) == before_cfg
        assert not (tmp_path / "config.json.tmp").exists()

    def test_successful_save_bytes_and_round_trip_unchanged(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = UserConfig(default_provider="openai",
                         default_model="gpt-4o-mini",
                         disable_fallbacks=True)
        save_config(cfg, path)
        # Byte-identical to the pre-E6-F07 format: json.dumps(indent=2),
        # no trailing newline.
        assert path.read_bytes() == json.dumps(
            cfg.to_dict(), indent=2).encode("utf-8")
        assert load_config(path) == cfg
        assert not (tmp_path / "config.json.tmp").exists()

    def test_save_creates_missing_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "config.json"
        cfg = UserConfig()
        save_config(cfg, path)
        assert load_config(path) == cfg


# ---------------------------------------------------------------------------
# load_config: corrupt vs unreadable
# ---------------------------------------------------------------------------

class TestLoadConfigCorruption:
    def test_corrupt_file_is_quarantined_and_defaults_used(
            self, tmp_path, caplog):
        path = tmp_path / "config.json"
        garbage = b'{"default_provider": "openai", "default_model": "gpt-'
        path.write_bytes(garbage)

        with caplog.at_level(logging.ERROR, logger="aether_engine.config"):
            loaded = load_config(path)

        assert loaded == UserConfig()
        # The evidence is preserved under the quarantine name, not
        # silently overwritten by defaults; the live path is clear so
        # the next save starts clean.
        assert not path.exists()
        quarantined = list(tmp_path.glob("config.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == garbage
        assert any("corrupt" in r.getMessage() for r in caplog.records)

        # A subsequent successful save is not blocked by the
        # quarantined copy, and the saved values load back.
        cfg = UserConfig(default_provider="openai",
                         default_model="gpt-4o-mini")
        save_config(cfg, path)
        assert load_config(path) == cfg
        assert len(list(tmp_path.glob("config.json.corrupt-*"))) == 1

    def test_unparseable_nonjson_file_is_quarantined(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_bytes(b"this is not json at all")
        assert load_config(path) == UserConfig()
        assert not path.exists()
        assert len(list(tmp_path.glob("config.json.corrupt-*"))) == 1

    def test_unreadable_file_is_left_untouched(self, tmp_path, monkeypatch,
                                                caplog):
        path = tmp_path / "config.json"
        cfg = UserConfig(default_provider="openai",
                         default_model="gpt-4o-mini")
        save_config(cfg, path)
        before_bytes = path.read_bytes()

        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text

        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError(
                    errno.EACCES, "Permission denied (injected)")
            return real_read_bytes(self, *args, **kwargs)

        def denied_text(self, *args, **kwargs):
            if self == path:
                raise PermissionError(
                    errno.EACCES, "Permission denied (injected)")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", denied)
        monkeypatch.setattr(Path, "read_text", denied_text)
        with caplog.at_level(logging.ERROR, logger="aether_engine.config"):
            loaded = load_config(path)
        monkeypatch.undo()

        # Defaults for the session, but an unreadable file is NOT
        # corruption evidence: never quarantined, never overwritten.
        assert loaded == UserConfig()
        assert path.read_bytes() == before_bytes
        assert list(tmp_path.glob("config.json.corrupt-*")) == []
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_missing_file_loads_defaults_without_quarantine(self, tmp_path):
        path = tmp_path / "config.json"
        assert load_config(path) == UserConfig()
        assert list(tmp_path.glob("config.json.corrupt-*")) == []


# ---------------------------------------------------------------------------
# CONFIG_UPDATE handler: save failure rolls back and replies
# ---------------------------------------------------------------------------

class TestUpdateSaveFailure:
    def test_failed_update_rolls_back_replies_and_preserves_file(
            self, config_env, monkeypatch):
        config_path = config_env["config_path"]
        before_bytes = config_path.read_bytes()
        switch = {"armed": False}
        _arm_partial_write(monkeypatch, switch)

        with _connect() as client, _ws_connect(client) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.type == EventType.HELLO
            assert engine_state.user_config.disable_fallbacks is True

            switch["armed"] = True
            ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_REQUEST,
                payload={"config": {
                    "disable_fallbacks": False,
                    "fallback_chain": ["google_gemini"],
                }},
            ).to_json())
            resp = _recv_until_timeout(ws, EventType.CONFIG_UPDATE_RESPONSE)
            switch["armed"] = False

            # Exactly one reply, and it is a failure in the existing
            # vocabulary (the F05 refusal shape).
            assert resp.payload["success"] is False
            assert resp.payload["error"]

            # Live state rolled back to the pre-handler values and the
            # file on disk is byte-identical — the failed save was a
            # non-event.
            assert engine_state.user_config.disable_fallbacks is True
            assert engine_state.user_config.fallback_chain == []
            assert config_path.read_bytes() == before_bytes

            # The session is alive and the save path recovers: the
            # same update now succeeds and persists. (If a duplicate
            # failure reply had been queued, this receive would have
            # returned it instead.)
            ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_REQUEST,
                payload={"config": {"disable_fallbacks": False}},
            ).to_json())
            resp2 = _recv_until(ws, EventType.CONFIG_UPDATE_RESPONSE)
            assert resp2.payload["success"] is True
            assert engine_state.user_config.disable_fallbacks is False
            assert load_config(config_path).disable_fallbacks is False

    def test_validation_refusal_is_not_a_save_failure(
            self, config_env, monkeypatch):
        # Control (E6-F05 ordering preserved): a malformed handed-off
        # field is refused BEFORE anything is applied or saved — the
        # refusal must not be confused with a save failure, and the
        # valid field riding along in the same payload is not applied.
        config_path = config_env["config_path"]
        before_bytes = config_path.read_bytes()

        with _connect() as client, _ws_connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_REQUEST,
                payload={"config": {
                    "supervisor_delegation_cap": 5000,  # bound is 0..1000
                    "disable_fallbacks": False,
                }},
            ).to_json())
            resp = _recv_until(ws, EventType.CONFIG_UPDATE_RESPONSE)
            assert resp.payload["success"] is False
            assert "supervisor_delegation_cap" in resp.payload["error"]
            assert engine_state.user_config.disable_fallbacks is True
            assert config_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# CONFIG_RESET handler: save failure rolls back and replies
# ---------------------------------------------------------------------------

class TestResetSaveFailure:
    def test_failed_reset_rolls_back_replies_and_preserves_file(
            self, config_env, monkeypatch):
        config_path = config_env["config_path"]
        before_bytes = config_path.read_bytes()
        switch = {"armed": False}
        _arm_partial_write(monkeypatch, switch)

        with _connect() as client, _ws_connect(client) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.type == EventType.HELLO
            assert hello.payload["active_provider"] == "openai"

            switch["armed"] = True
            ws.send_text(Event(
                type=EventType.CONFIG_RESET_REQUEST, payload={}).to_json())
            resp = _recv_until_timeout(ws, EventType.CONFIG_RESET_RESPONSE)
            switch["armed"] = False

            assert resp.payload["success"] is False
            assert resp.payload["error"]

            # Nothing was reset: live config is the pre-reset one
            # (not the google_gemini defaults), the derived live
            # fields were NOT resynced, and the file is intact.
            assert engine_state.user_config.default_provider == "openai"
            assert engine_state.user_config.disable_fallbacks is True
            assert engine_state.active_provider == "openai"
            assert config_path.read_bytes() == before_bytes

            # Recovery: a second reset with a healthy disk succeeds
            # and resyncs exactly as E6-F04 requires.
            ws.send_text(Event(
                type=EventType.CONFIG_RESET_REQUEST, payload={}).to_json())
            resp2 = _recv_until(ws, EventType.CONFIG_RESET_RESPONSE)
            assert resp2.payload["success"] is True
            assert engine_state.user_config == UserConfig()
            assert engine_state.active_provider == "google_gemini"
            assert load_config(config_path) == UserConfig()
