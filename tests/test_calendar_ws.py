"""Calendar C5 engine side: config-update validation + list handler."""

import json
import time

import pytest

from aether_engine.calendar.store import EventStore
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.routes.ws_calendar import handle_calendar_list_request


def test_config_updates_accept_calendar_fields():
    cleaned = validate_handed_off_updates(
        {
            "calendar_enabled": False,
            "calendar_reminder_day_before": True,
            "calendar_reminder_lead_minutes": 45,
        }
    )
    assert cleaned == {
        "calendar_enabled": False,
        "calendar_reminder_day_before": True,
        "calendar_reminder_lead_minutes": 45,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"calendar_enabled": "yes"},  # a truthy string is not a boolean
        {"calendar_reminder_day_before": 1},
        {"calendar_reminder_lead_minutes": -5},
        {"calendar_reminder_lead_minutes": 99999},
        {"calendar_reminder_lead_minutes": True},  # bool is not a count
    ],
)
def test_config_updates_refuse_bad_calendar_values(bad):
    with pytest.raises(ValueError):
        validate_handed_off_updates(bad)


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


@pytest.mark.asyncio
async def test_calendar_list_handler_returns_events(tmp_path, monkeypatch):
    store = EventStore(tmp_path / "calendar.db")
    store.add_event("PTM", time.time() + 3600)
    monkeypatch.setattr(
        "aether_engine.routes.ws_calendar.get_calendar_store", lambda: store
    )
    conn = _FakeConn()
    await handle_calendar_list_request(conn, _FakeMsg({"limit": 5}))
    assert len(conn.ws.sent) == 1
    reply = json.loads(conn.ws.sent[0])
    assert reply["type"] == "CALENDAR_LIST_RESPONSE"
    assert reply["payload"]["success"] is True
    assert [e["title"] for e in reply["payload"]["events"]] == ["PTM"]
    assert reply["payload"]["events"][0]["start"].startswith("20")


@pytest.mark.asyncio
async def test_calendar_list_handler_answers_errors(monkeypatch):
    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(
        "aether_engine.routes.ws_calendar.get_calendar_store", boom
    )
    conn = _FakeConn()
    await handle_calendar_list_request(conn, _FakeMsg())
    reply = json.loads(conn.ws.sent[0])
    assert reply["payload"]["success"] is False
    assert "db gone" in reply["payload"]["error"]
