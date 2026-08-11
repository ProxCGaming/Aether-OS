"""Unit and integration tests for ProviderHealthBadge and HUD health transitions."""
import pytest
from PySide6.QtWidgets import QApplication

from aether_common.contracts import Event, EventType
from aether_engine.routing.health import ProviderHealthManager, ProviderHealthState
from aether_ui.hud import HudWidget, _format_provider_name
from aether_ui.state import UIStateMachine
from aether_ui.widgets.provider_health_badge import ProviderHealthBadge


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_provider_health_badge_initial_state(qapp):
    badge = ProviderHealthBadge()
    assert badge._status == "unknown"
    assert badge.lbl_text.text() == "Standby"
    assert "Standby" in badge.toolTip()


def test_provider_health_badge_healthy_with_latency(qapp):
    badge = ProviderHealthBadge()
    badge.update_health(status="healthy", latency_ms=145.6, provider_name="Google Gemini")
    assert badge._status == "healthy"
    assert badge.lbl_text.text() == "Healthy (145ms)"
    assert "Google Gemini" in badge.toolTip()
    assert "145.6 ms" in badge.toolTip()


def test_provider_health_badge_degraded_and_offline(qapp):
    badge = ProviderHealthBadge()
    badge.update_health(
        status="degraded",
        latency_ms=0.0,
        provider_name="OpenAI",
        last_error="Rate limited (429)",
    )
    assert badge._status == "degraded"
    assert badge.lbl_text.text() == "Degraded"
    assert "Rate limited (429)" in badge.toolTip()

    badge.update_health(
        status="offline",
        latency_ms=0.0,
        provider_name="OpenAI",
        last_error="Invalid API key (401)",
    )
    assert badge._status == "offline"
    assert badge.lbl_text.text() == "Offline"
    assert "Invalid API key (401)" in badge.toolTip()


def test_hud_hello_event_initializes_health(qapp):
    sm = UIStateMachine()
    hud = HudWidget(sm)

    # Initial state is Standby
    assert hud.health_badge.lbl_text.text() == "Standby"

    # Hello event with health status
    hello_event = Event(
        type=EventType.HELLO,
        payload={
            "message": "Engine Ready",
            "active_provider": "google_gemini",
            "active_model": "gemini-2.0-flash",
            "health": {
                "google_gemini": {
                    "provider": "google_gemini",
                    "status": "healthy",
                    "latency_ms": 120.0,
                    "last_successful_request": 1000.0,
                }
            },
        },
    )
    hud.log_event(hello_event)
    assert hud.health_badge._status == "healthy"
    assert hud.health_badge.lbl_text.text() == "Healthy (120ms)"


def test_hud_task_completed_updates_health_badge(qapp):
    sm = UIStateMachine()
    hud = HudWidget(sm)
    hud.set_active_model("google_gemini", "gemini-2.0-flash")

    comp_event = Event(
        type=EventType.TASK_COMPLETED,
        payload={
            "task_id": "test-123",
            "response": "Hello world",
            "latency_ms": 85.4,
            "provider": "google_gemini",
        },
    )
    hud.log_event(comp_event)
    assert hud.health_badge._status == "healthy"
    assert hud.health_badge.lbl_text.text() == "Healthy (85ms)"
    assert "85.4 ms" in hud.health_badge.toolTip()


def test_hud_task_failed_updates_health_badge(qapp):
    sm = UIStateMachine()
    hud = HudWidget(sm)
    hud.set_active_model("google_gemini", "gemini-2.0-flash")

    fail_event = Event(
        type=EventType.TASK_FAILED,
        payload={
            "task_id": "test-123",
            "error": "Quota exceeded",
            "retriable": False,
            "provider": "google_gemini",
        },
    )
    hud.log_event(fail_event)
    assert hud.health_badge._status == "offline"
    assert hud.health_badge.lbl_text.text() == "Offline"
    assert "Quota exceeded" in hud.health_badge.toolTip()


def test_hud_provider_health_update_event(qapp):
    sm = UIStateMachine()
    hud = HudWidget(sm)
    hud.set_active_model("openai", "gpt-4o-mini")

    health_update = Event(
        type=EventType.PROVIDER_HEALTH_UPDATE,
        payload={
            "provider": "openai",
            "health": {
                "openai": {
                    "provider": "openai",
                    "status": "healthy",
                    "latency_ms": 230.0,
                }
            },
        },
    )
    hud.log_event(health_update)
    assert hud.health_badge._status == "healthy"
    assert hud.health_badge.lbl_text.text() == "Healthy (230ms)"


def test_hud_provider_validation_event(qapp):
    sm = UIStateMachine()
    hud = HudWidget(sm)
    hud.set_active_model("google_gemini", "gemini-2.0-flash")

    val_event = Event(
        type=EventType.PROVIDER_VALIDATE_RESPONSE,
        payload={
            "provider": "google_gemini",
            "status": "connected",
            "latency_ms": 95.0,
            "message": "Validated successfully",
        },
    )
    hud.log_event(val_event)
    assert hud.health_badge._status == "healthy"
    assert hud.health_badge.lbl_text.text() == "Healthy (95ms)"


def test_provider_health_manager_callbacks():
    notified = []

    def on_change(provider, state):
        notified.append((provider, state.status, state.latency_ms))

    mgr = ProviderHealthManager(on_status_change=on_change)
    mgr.record_success("google_gemini", 110.5)
    assert len(notified) == 1
    assert notified[0] == ("google_gemini", "healthy", 110.5)

    mgr.record_failure("google_gemini", "Timeout", is_retriable=True)
    assert len(notified) == 2
    assert notified[1] == ("google_gemini", "degraded", 0.0)
