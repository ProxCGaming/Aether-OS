"""Calendar tools + registry wiring + config round-trip (C1)."""

from datetime import datetime, timedelta

import pytest

from aether_engine.calendar.store import EventStore
from aether_engine.config import UserConfig
from aether_engine.tools.calendar_tools import create_calendar_tools
from aether_engine.tools.registry import ToolRisk, build_tool_registry


@pytest.fixture()
def tools(tmp_path):
    store = EventStore(tmp_path / "calendar.db")
    return {t.name: t for t in create_calendar_tools(store)}, store


def _future_iso(hours: float = 26) -> str:
    return (datetime.now().astimezone() + timedelta(hours=hours)).isoformat()


def test_tool_risks(tools):
    by_name, _ = tools
    assert by_name["add_event"].risk == ToolRisk.WRITE
    assert by_name["list_events"].risk == ToolRisk.READ_ONLY
    assert by_name["cancel_event"].risk == ToolRisk.WRITE


def test_add_list_cancel_flow(tools):
    by_name, _ = tools
    reply = by_name["add_event"].execute_fn(title="PTM", start=_future_iso())
    assert reply.startswith("Added to calendar: PTM")
    listed = by_name["list_events"].execute_fn()
    assert "PTM" in listed
    cancelled = by_name["cancel_event"].execute_fn(title="PTM")
    assert cancelled.startswith("Cancelled: PTM")
    assert by_name["list_events"].execute_fn() == "No upcoming events on the calendar."


def test_add_reports_conflict(tools):
    by_name, _ = tools
    start = _future_iso(30)
    by_name["add_event"].execute_fn(title="First", start=start)
    reply = by_name["add_event"].execute_fn(title="Second", start=start)
    assert "overlaps with" in reply and "First" in reply


def test_add_rejects_garbage_time(tools):
    by_name, _ = tools
    reply = by_name["add_event"].execute_fn(title="X", start="next teusday lol")
    assert reply.startswith("Error:")


def test_cancel_ambiguous_title_lists_options(tools):
    by_name, _ = tools
    by_name["add_event"].execute_fn(title="Team call", start=_future_iso(10))
    by_name["add_event"].execute_fn(title="Team call", start=_future_iso(50))
    reply = by_name["cancel_event"].execute_fn(title="team call")
    assert "More than one" in reply


def test_registry_contains_calendar_tools():
    registry = build_tool_registry()
    names = {t.name for t in registry.get_tools()}
    assert {"add_event", "list_events", "cancel_event"} <= names


def test_config_roundtrip_calendar_fields():
    cfg = UserConfig()
    assert cfg.calendar_enabled is True
    assert cfg.calendar_reminder_day_before is True
    assert cfg.calendar_reminder_lead_minutes == 30
    cfg.calendar_enabled = False
    cfg.calendar_reminder_lead_minutes = 45
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.calendar_enabled is False
    assert restored.calendar_reminder_day_before is True
    assert restored.calendar_reminder_lead_minutes == 45
    # Absent keys fall back to the defaults.
    fresh = UserConfig.from_dict({})
    assert fresh.calendar_enabled is True
    assert fresh.calendar_reminder_lead_minutes == 30
