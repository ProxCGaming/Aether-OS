"""S22 / SPEC-6B C3 (FF5-06): the Calendar panel's create / cancel.

Pins what the spec note promises for Batch 5 board 5 — the panel
calls create/cancel directly (not via chat extraction):

* ``calendar_create(title, start, duration_s)`` →
  ``{event, conflicts: [{event_id, title, start, end}…]}`` — a
  non-empty conflicts list is a WARNING reply, not an error:
  without ``accept_conflicts`` nothing is stored (saved=False);
  saving anyway is the same call with ``accept_conflicts: true``;
* ``calendar_cancel(event_id)`` → ``{cancelled: event_id}``;
  cancellation is durable across restart (store-level delete),
  and a cancel of a missing id returns an honest not-found —
  never a fake success.

The store itself is a tmp SQLite file; handler tests follow the
test_calendar_ws.py fake-conn pattern.
"""
import json
import time

from aether_engine.calendar.store import EventStore
from aether_engine.routes.ws_calendar import (
    handle_calendar_cancel_request,
    handle_calendar_create_request,
)

NOW = time.time()


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _FakeConn:
    def __init__(self):
        self.ws = _FakeWs()


class _FakeMsg:
    def __init__(self, payload=None):
        self.request_id = "req-1"
        self.payload = payload or {}


def _patch_store(monkeypatch, store):
    monkeypatch.setattr(
        "aether_engine.routes.ws_calendar.get_calendar_store", lambda: store)


async def _create(conn, payload):
    await handle_calendar_create_request(conn, _FakeMsg(payload))
    return json.loads(conn.ws.sent[-1])


async def _cancel(conn, payload):
    await handle_calendar_cancel_request(conn, _FakeMsg(payload))
    return json.loads(conn.ws.sent[-1])


# ----------------------------------------------------------------------
# Create: no conflict → saved, conflicts listed empty
# ----------------------------------------------------------------------

async def test_create_without_conflict_saves(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    _patch_store(monkeypatch, store)
    conn = _FakeConn()

    reply = await _create(conn, {
        "title": "Dentist", "start_ts": NOW + 3600, "duration_s": 1800})
    assert reply["type"] == "CALENDAR_CREATE_RESPONSE"
    payload = reply["payload"]
    assert payload["success"] is True
    assert payload["saved"] is True
    assert payload["conflicts"] == []
    assert payload["event"]["title"] == "Dentist"
    assert payload["event"]["source"] == "manual"
    # The stored window is start + duration.
    stored = store.get_event(payload["event"]["id"])
    assert stored.end_ts == stored.start_ts + 1800


async def test_create_accepts_an_iso_start(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    _patch_store(monkeypatch, store)
    conn = _FakeConn()
    from datetime import datetime, timedelta

    start = datetime.now().astimezone() + timedelta(days=1)
    reply = await _create(conn, {
        "title": "Flight", "start": start.isoformat(), "duration_s": 3600})
    payload = reply["payload"]
    assert payload["saved"] is True
    assert abs(payload["event"]["start_ts"] - start.timestamp()) < 1.0


# ----------------------------------------------------------------------
# Create: conflict → warning (nothing stored) until accepted
# ----------------------------------------------------------------------

async def test_create_conflict_warns_and_stores_nothing(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    existing, _ = store.add_event("PTM", NOW + 3600, NOW + 7200)
    _patch_store(monkeypatch, store)
    conn = _FakeConn()

    reply = await _create(conn, {
        "title": "Dentist", "start_ts": NOW + 5400, "duration_s": 3600})
    payload = reply["payload"]
    # A warning, not an error — and nothing was saved.
    assert payload["success"] is True
    assert payload["saved"] is False
    assert payload["event"] is None
    assert "error" not in payload
    assert payload["conflicts"] == [{
        "event_id": existing.id,
        "title": "PTM",
        "start": existing.start_dt.isoformat(),
        "end": existing.end_dt.isoformat(),
    }]
    assert [e.title for e in store.upcoming(NOW)] == ["PTM"]

    # "Save anyway": the same call with accept_conflicts=True.
    reply = await _create(conn, {
        "title": "Dentist", "start_ts": NOW + 5400, "duration_s": 3600,
        "accept_conflicts": True})
    payload = reply["payload"]
    assert payload["success"] is True
    assert payload["saved"] is True
    assert payload["event"]["title"] == "Dentist"
    assert [c["event_id"] for c in payload["conflicts"]] == [existing.id]
    assert sorted(e.title for e in store.upcoming(NOW)) == ["Dentist", "PTM"]


async def test_create_refuses_bad_input(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    _patch_store(monkeypatch, store)
    conn = _FakeConn()

    reply = await _create(conn, {"title": "  ", "start_ts": NOW + 60})
    assert reply["payload"]["success"] is False
    assert reply["payload"]["saved"] is False
    assert "title" in reply["payload"]["error"]

    reply = await _create(conn, {
        "title": "X", "start_ts": NOW + 60, "duration_s": -5})
    assert reply["payload"]["success"] is False
    assert store.upcoming(NOW) == []


# ----------------------------------------------------------------------
# Cancel: durable across restart; missing id is an honest not-found
# ----------------------------------------------------------------------

async def test_cancel_is_durable_across_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "calendar.db"
    store = EventStore(db_path)
    event, _ = store.add_event("PTM", NOW + 3600)
    _patch_store(monkeypatch, store)
    conn = _FakeConn()

    reply = await _cancel(conn, {"event_id": event.id})
    assert reply["type"] == "CALENDAR_CANCEL_RESPONSE"
    assert reply["payload"] == {"success": True, "cancelled": event.id}

    # A fresh store over the same file — the restart view — agrees
    # the event is gone.
    assert EventStore(db_path).get_event(event.id) is None


async def test_cancel_of_a_missing_id_is_an_honest_not_found(
        tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    _patch_store(monkeypatch, store)
    conn = _FakeConn()

    reply = await _cancel(conn, {"event_id": "no-such-event"})
    payload = reply["payload"]
    assert payload["success"] is False
    assert payload["cancelled"] is None
    assert "nothing was cancelled" in payload["error"]
