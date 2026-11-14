"""Master build S20: desktop actuation — policy gate, envelopes,
control lane, provider contract, vision grounding (owner O1).

S19 built observe; S20 builds CONTROL on the same seam, layered
as the owner decided: UIA invoke/set-value by element first,
vision grounding only for UIA-blind targets (screenshot
discarded after the call), pixel mouse/keyboard as its own
switchable, per-session-consent class. The hard-confirm class
(close_or_quit + system) mirrors browser payments: no envelope,
no remembered grant, no Allow-freely — anywhere.

The Windows backends cannot run in this environment (Linux VM,
no uiautomation package — the standing S19/S20 caveat), so
everything here is contract-tested against fakes:

- the policy taxonomy matrix (category × mode, hard-confirm
  refusing envelopes/freely at every layer), envelope bounds
  (count, expiry, app/category mismatch), per-app deny;
- the actuator contract against a fake actuation adapter
  (invoke, set-value, clipboard, window focus/close, pixel
  click/type) and target resolution against a fake read adapter;
- vision grounding against a fake capture + fake vision model:
  coordinates used, screenshot never persisted (file opens are
  rigged to explode), low confidence refused unless UIA
  verification anchors it;
- the live control lane: stop refuses a task's later actions
  while the task continues, pause/user states refuse in place;
- the graph gate (pause node with a mocked interrupt, the
  browser tests' pattern): hard-confirm always pauses,
  envelope/remember grants land only through an approval, the
  final-args gate re-binds changed actions;
- the offering pin: desktop_act absent — and the prompt
  byte-identical — unless control is fully permitted;
- the config surface: strict validation (incl. the
  hard-confirm mode refusal with its reason), the
  acknowledgement rule at the CONFIG_UPDATE handler, and the
  project-layer strip (a cloned repo can never enable control).
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.cost.ledger import usage_context
from aether_engine.desktop import (
    DesktopActuator,
    UnavailableDesktopProvider,
    WindowsUiaProvider,
    control_permitted,
    set_desktop_actuator,
)
from aether_engine.desktop import capture as capture_module
from aether_engine.desktop import policy as desktop_policy
from aether_engine.desktop import vision as vision_module
from aether_engine.desktop.capture import _encode_png
from aether_engine.desktop.control import (
    DesktopControl,
    get_desktop_control,
    reset_desktop_control,
)
from aether_engine.desktop.grants import (
    DesktopGrantStore,
    reset_default_grant_store,
    set_default_grant_store,
)
from aether_engine.desktop.policy import (
    DesktopEnvelopeStore,
    DesktopPolicy,
    classify_desktop_action,
    clean_category_modes,
    evaluate_desktop_action,
    grant_envelope_from_confirmation,
    policy_from_user_config,
    reset_desktop_envelope_store,
    set_desktop_policy_loader,
)
from aether_engine.desktop.tools import _execute_desktop_act
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.graph import tool_router
from aether_engine.langgraph.nodes.pause import (
    dummy_pause_node,
    make_desktop_final_args_gate,
)
from aether_engine.langgraph.nodes.researcher import RESEARCHER_SPEC
from aether_engine.langgraph.nodes.runner import run_specialist
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.registry import ToolRegistry, build_tool_registry


# --------------------------------------------------------------------
# Fakes + fixtures
# --------------------------------------------------------------------
def _element(name="", role="Text", value=None, focused=False, children=(),
             automation_id=None, bounds=(0, 0, 100, 20), can_invoke=False,
             can_set_value=False, is_password=False):
    return {
        "name": name, "role": role, "value": value,
        "automation_id": automation_id, "focused": focused,
        "bounds": bounds, "children": list(children),
        "can_invoke": can_invoke, "can_set_value": can_set_value,
        "is_password": is_password,
    }


class FakeReadAdapter:
    """A RawDesktopAdapter over fixed raw dicts (S19's shape)."""

    def __init__(self, foreground=None, windows=(), tree=None):
        self._foreground = foreground
        self._windows = list(windows)
        self._tree = tree

    def foreground_window(self):
        return self._foreground

    def list_windows(self):
        return list(self._windows)

    def read_tree(self, handle, *, max_depth, max_elements):
        return self._tree


class FakeActuationAdapter:
    """A RawActuationAdapter that records every call."""

    def __init__(self, clipboard_text="fake clipboard"):
        self.calls = []
        self.clipboard_text = clipboard_text

    def _record(self, name, *args):
        self.calls.append((name, *args))
        return {"ok": True, "detail": f"{name} done"}

    def invoke_element(self, handle, matcher):
        return self._record("invoke_element", handle, matcher)

    def set_element_value(self, handle, matcher, value):
        return self._record("set_element_value", handle, matcher, value)

    def focus_window(self, handle):
        return self._record("focus_window", handle)

    def close_window(self, handle):
        return self._record("close_window", handle)

    def clipboard_get(self):
        self.calls.append(("clipboard_get",))
        return {"ok": True, "text": self.clipboard_text, "detail": ""}

    def clipboard_set(self, text):
        self.clipboard_text = text
        return self._record("clipboard_set", text)

    def mouse_click(self, x, y):
        return self._record("mouse_click", x, y)

    def type_text(self, text):
        return self._record("type_text", text)

    def key_press(self, keys):
        return self._record("key_press", keys)


def _word_tree():
    return _element("Document1 - Word", "Window", bounds=(0, 0, 1200, 800),
                    children=[
        _element("Ribbon", "ToolBar", children=[
            _element("Save", "Button", automation_id="btn-save",
                     can_invoke=True, bounds=(10, 10, 60, 40)),
            _element("Close", "Button", can_invoke=True,
                     bounds=(1150, 10, 1190, 40)),
        ]),
        _element("Document", "Edit", value="Dear team,", focused=True,
                 can_set_value=True, bounds=(100, 100, 1100, 700)),
        _element("Password", "Edit", can_set_value=True, is_password=True,
                 bounds=(100, 720, 400, 750)),
        _element("Shut Down", "Button", can_invoke=True,
                 bounds=(10, 760, 120, 790)),
    ])


def _word_read():
    return FakeReadAdapter(
        foreground={"title": "Document1 - Word", "app": "WINWORD.EXE",
                    "handle": 101},
        windows=[
            {"title": "Document1 - Word", "app": "WINWORD.EXE",
             "handle": 101},
            {"title": "Inbox - Outlook", "app": "OUTLOOK.EXE",
             "handle": 102},
        ],
        tree=_word_tree(),
    )


def _actuator(read=None, act=None):
    return DesktopActuator(
        WindowsUiaProvider(read or _word_read()),
        act if act is not None else FakeActuationAdapter())


def _uc(**overrides):
    """A UserConfig with control fully permitted, plus overrides."""
    values = {
        "computer_use_enabled": True,
        "computer_use_observe": True,
        "computer_use_control": True,
        "computer_use_control_ack": True,
    }
    values.update(overrides)
    return UserConfig(**values)


def _policy(**overrides):
    return policy_from_user_config(_uc(**overrides))


class _RecordingAudit:
    def __init__(self):
        self.events = []

    def log_event(self, action, details=None):
        self.events.append((action, details or {}))

    def actions(self):
        return [a for a, _ in self.events]


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    """Isolated desktop state: fake actuator installed, control
    lane + envelope store reset, grants in a tmp store, the
    tool's policy loader pointed at a mutable config holder, and
    the control lane's audit logger stubbed (no real audit file)."""
    act = FakeActuationAdapter()
    actuator = _actuator(act=act)
    set_desktop_actuator(actuator)
    reset_desktop_control()
    reset_desktop_envelope_store()
    store = DesktopGrantStore(tmp_path / "grants.db")
    set_default_grant_store(store)
    holder = {"uc": _uc()}
    set_desktop_policy_loader(
        lambda: policy_from_user_config(holder["uc"]))
    import aether_engine.desktop.control as control_module
    audit = _RecordingAudit()
    monkeypatch.setattr(
        control_module, "_audit_logger_singleton", audit)
    yield SimpleNamespace(
        act=act, actuator=actuator, grants=store, holder=holder,
        audit=audit)
    set_desktop_actuator(None)
    set_desktop_policy_loader(None)
    reset_desktop_control()
    reset_desktop_envelope_store()
    reset_default_grant_store()
    vision_module.set_vision_provider_resolver(None)
    capture_module.set_capture_provider(None)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------
# Policy taxonomy
# --------------------------------------------------------------------
class TestPolicyTaxonomy:
    def test_default_policy_is_everything_off_and_confirming(self):
        policy = policy_from_user_config(UserConfig())
        assert policy.control_permitted is False
        for category in desktop_policy.CATEGORIES:
            assert policy.mode_for(category) == "always_confirm"

    def test_hard_confirm_mode_is_forced_at_every_layer(self):
        # A stored allow_freely (hand-edited config / legacy value)
        # is normalised by the cleaner AND re-forced by mode_for.
        cleaned = clean_category_modes(
            {"close_or_quit": "allow_freely", "system": "allow_with_envelope"})
        assert cleaned["close_or_quit"] == "always_confirm"
        assert cleaned["system"] == "always_confirm"
        raw = DesktopPolicy(categories={"close_or_quit": "allow_freely"})
        assert raw.mode_for("close_or_quit") == "always_confirm"
        assert raw.mode_for("system") == "always_confirm"

    def test_config_validation_refuses_looser_hard_confirm_modes(self):
        with pytest.raises(ValueError, match="always confirm individually"):
            validate_handed_off_updates(
                {"computer_use_category_modes": {"system": "allow_freely"}})
        with pytest.raises(ValueError, match="always confirm individually"):
            validate_handed_off_updates(
                {"computer_use_category_modes":
                 {"close_or_quit": "allow_with_envelope"}})
        # always_confirm itself stores fine, and so do free modes
        # for ordinary categories.
        cleaned = validate_handed_off_updates(
            {"computer_use_category_modes":
             {"close_or_quit": "always_confirm", "interact": "allow_freely"}})
        assert cleaned["computer_use_category_modes"]["interact"] == "allow_freely"

    def test_classification_matrix(self):
        actuator = _actuator()
        save = actuator.resolve_now({"name": "Save", "role": "Button"})
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke",
                            "element": {"name": "Save", "role": "Button"}},
            resolved=save)
        assert (action.category, action.app, action.via) == (
            "interact", "WINWORD.EXE", "uia")

        close = actuator.resolve_now({"name": "Close", "role": "Button"})
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke",
                            "element": {"name": "Close"}},
            resolved=close)
        assert action.category == "close_or_quit"

        shutdown = actuator.resolve_now({"name": "Shut Down"})
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke",
                            "element": {"name": "Shut Down"}},
            resolved=shutdown)
        assert action.category == "system"

        doc = actuator.resolve_now({"name": "Document", "role": "Edit"})
        action = classify_desktop_action(
            "desktop_act", {"action": "set_value",
                            "element": {"name": "Document"}, "value": "x"},
            resolved=doc)
        assert action.category == "type_into"
        assert action.force_confirm is False
        # Typed text never enters the identity (M03 analogue).
        assert "x" not in action.identity

        password = actuator.resolve_now({"name": "Password", "role": "Edit"})
        action = classify_desktop_action(
            "desktop_act", {"action": "set_value",
                            "element": {"name": "Password"}, "value": "s"},
            resolved=password)
        assert action.category == "type_into"
        assert action.force_confirm is True

        window = actuator.resolve_window_now(app="OUTLOOK.EXE")
        action = classify_desktop_action(
            "desktop_act", {"action": "focus_window", "app": "OUTLOOK.EXE"},
            resolved=window)
        assert (action.category, action.app) == ("open", "OUTLOOK.EXE")
        action = classify_desktop_action(
            "desktop_act", {"action": "close_window", "app": "OUTLOOK.EXE"},
            resolved=window)
        assert action.category == "close_or_quit"

        action = classify_desktop_action(
            "desktop_act", {"action": "key_press", "keys": "alt+f4"})
        assert action.category == "close_or_quit"
        action = classify_desktop_action(
            "desktop_act", {"action": "key_press", "keys": "win+r"})
        assert action.category == "system"
        action = classify_desktop_action(
            "desktop_act", {"action": "key_press", "keys": "ctrl+s"})
        assert (action.category, action.via) == ("interact", "pixel")

        action = classify_desktop_action(
            "desktop_act", {"action": "clipboard_get"})
        assert action.category == "clipboard"
        action = classify_desktop_action(
            "desktop_act", {"action": "clipboard_set", "text": "t"})
        assert action.category == "clipboard"

        action = classify_desktop_action(
            "desktop_act", {"action": "ground_click",
                            "description": "the red button"})
        assert (action.category, action.via) == ("interact", "vision")

        # Unresolvable element and unrecognised verbs are unknown,
        # and unknown confirms (pinned in the gate tests below).
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke",
                            "element": {"name": "Missing"}},
            resolved=actuator.resolve_now({"name": "Missing"}))
        assert action.category == "unknown"
        action = classify_desktop_action(
            "desktop_act", {"action": "explode"})
        assert action.category == "unknown"

    def test_gate_modes(self):
        actuator = _actuator()
        save = classify_desktop_action(
            "desktop_act", {"action": "invoke", "element": {"name": "Save"}},
            resolved=actuator.resolve_now({"name": "Save"}))
        # allow_freely allows.
        decision = evaluate_desktop_action(
            _policy(computer_use_category_modes={"interact": "allow_freely"}),
            save, task_id="t1")
        assert decision.verdict == "allow"
        # always_confirm confirms.
        decision = evaluate_desktop_action(_policy(), save, task_id="t1")
        assert (decision.verdict, decision.reason) == (
            "confirm", "always_confirm")
        # Envelope mode with no envelope confirms.
        decision = evaluate_desktop_action(
            _policy(computer_use_category_modes=
                    {"interact": "allow_with_envelope"}),
            save, task_id="t1")
        assert decision.verdict == "confirm"
        # Control not permitted denies before anything else.
        decision = evaluate_desktop_action(
            policy_from_user_config(UserConfig()), save, task_id="t1")
        assert (decision.verdict, decision.reason) == (
            "deny", "control_disabled")

    def test_hard_confirm_confirms_under_every_relaxation(self):
        # allow_freely stored, envelope live, remembered grant
        # live: a close action STILL confirms.
        store = DesktopEnvelopeStore()
        close = classify_desktop_action(
            "desktop_act", {"action": "close_window", "app": "WINWORD.EXE"},
            resolved=_actuator().resolve_window_now(app="WINWORD.EXE"))
        decision = evaluate_desktop_action(
            _policy(computer_use_category_modes=
                    {"interact": "allow_freely"}),
            close, task_id="t1", envelope_store=store,
            grant_covers=lambda app, cat: True)
        assert (decision.verdict, decision.reason) == (
            "confirm", "hard_confirm")

    def test_password_field_force_confirms_under_allow_freely(self):
        password = classify_desktop_action(
            "desktop_act", {"action": "set_value",
                            "element": {"name": "Password"}, "value": "s"},
            resolved=_actuator().resolve_now({"name": "Password"}))
        decision = evaluate_desktop_action(
            _policy(computer_use_category_modes=
                    {"type_into": "allow_freely"}),
            password, task_id="t1", grant_covers=lambda a, c: True)
        assert (decision.verdict, decision.reason) == (
            "confirm", "sensitive_target")


# --------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------
class TestEnvelopes:
    def _save_action(self):
        return classify_desktop_action(
            "desktop_act", {"action": "invoke", "element": {"name": "Save"}},
            resolved=_actuator().resolve_now({"name": "Save"}))

    def test_count_bound(self):
        store = DesktopEnvelopeStore()
        store.grant(task_id="t1", category="interact",
                    app="WINWORD.EXE", max_actions=2)
        policy = _policy(computer_use_category_modes=
                         {"interact": "allow_with_envelope"})
        action = self._save_action()
        assert evaluate_desktop_action(
            policy, action, task_id="t1", envelope_store=store).verdict == "allow"
        assert evaluate_desktop_action(
            policy, action, task_id="t1", envelope_store=store).verdict == "allow"
        # Third action: the envelope is spent, so it confirms.
        assert evaluate_desktop_action(
            policy, action, task_id="t1", envelope_store=store).verdict == "confirm"

    def test_expiry(self):
        store = DesktopEnvelopeStore()
        store.grant(task_id="t1", category="interact", app="WINWORD.EXE",
                    max_actions=20, ttl_seconds=3600, now=1000.0)
        policy = _policy(computer_use_category_modes=
                         {"interact": "allow_with_envelope"})
        action = self._save_action()
        assert evaluate_desktop_action(
            policy, action, task_id="t1", envelope_store=store,
            now=1000.0 + 3599).verdict == "allow"
        assert evaluate_desktop_action(
            policy, action, task_id="t1", envelope_store=store,
            now=1000.0 + 3601).verdict == "confirm"

    def test_app_and_category_mismatch_do_not_cover(self):
        store = DesktopEnvelopeStore()
        store.grant(task_id="t1", category="interact",
                    app="OUTLOOK.EXE", max_actions=20)
        policy = _policy(computer_use_category_modes=
                         {"interact": "allow_with_envelope"})
        # Different app than the envelope's.
        assert evaluate_desktop_action(
            policy, self._save_action(), task_id="t1",
            envelope_store=store).verdict == "confirm"
        # Different task entirely.
        store2 = DesktopEnvelopeStore()
        store2.grant(task_id="t2", category="interact",
                     app="WINWORD.EXE", max_actions=20)
        assert evaluate_desktop_action(
            policy, self._save_action(), task_id="t1",
            envelope_store=store2).verdict == "confirm"

    def test_envelopes_refuse_the_hard_confirm_class(self):
        store = DesktopEnvelopeStore()
        with pytest.raises(ValueError, match="hard-confirm"):
            store.grant(task_id="t1", category="close_or_quit")
        with pytest.raises(ValueError, match="hard-confirm"):
            store.grant(task_id="t1", category="system")

    def test_grant_from_confirmation_scopes_to_the_confirmed_action(self):
        # The spec only opts in/narrows the count; category + app
        # come from the action the owner actually confirmed, even
        # if a crafted spec claims a wider scope.
        store = DesktopEnvelopeStore()
        envelope = grant_envelope_from_confirmation(
            task_id="t1", action=self._save_action(),
            spec={"grant": True, "category": "clipboard",
                  "app": "EVIL.EXE", "max_actions": 999},
            envelope_store=store)
        assert envelope.category == "interact"
        assert envelope.app == "WINWORD.EXE"
        assert envelope.max_actions == \
            desktop_policy.DEFAULT_ENVELOPE_MAX_ACTIONS
        # No grant flag, no envelope.
        assert grant_envelope_from_confirmation(
            task_id="t1", action=self._save_action(), spec={},
            envelope_store=store) is None
        # A hard-confirm action can never seed one.
        close = classify_desktop_action(
            "desktop_act", {"action": "close_window", "app": "WINWORD.EXE"},
            resolved=_actuator().resolve_window_now(app="WINWORD.EXE"))
        assert grant_envelope_from_confirmation(
            task_id="t1", action=close, spec={"grant": True},
            envelope_store=store) is None

    def test_envelopes_disabled_falls_back_to_grants_then_confirm(self):
        policy = _policy(
            computer_use_envelopes_enabled=False,
            computer_use_category_modes={"interact": "allow_with_envelope"})
        action = self._save_action()
        decision = evaluate_desktop_action(policy, action, task_id="t1")
        assert (decision.verdict, decision.reason) == (
            "confirm", "envelopes_disabled")
        decision = evaluate_desktop_action(
            policy, action, task_id="t1",
            grant_covers=lambda app, cat: True)
        assert (decision.verdict, decision.reason) == (
            "allow", "remembered_grant")


# --------------------------------------------------------------------
# Per-app deny + remembered grants
# --------------------------------------------------------------------
class TestAppDenyAndGrants:
    def test_deny_beats_modes_envelopes_and_grants(self):
        policy = _policy(
            computer_use_category_modes={"interact": "allow_freely"},
            computer_use_app_rules=[
                {"app": "winword.exe", "verdict": "deny"}])
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke", "element": {"name": "Save"}},
            resolved=_actuator().resolve_now({"name": "Save"}))
        decision = evaluate_desktop_action(
            policy, action, task_id="t1",
            grant_covers=lambda a, c: True)
        assert (decision.verdict, decision.reason) == ("deny", "app_denied")

    def test_deny_refuses_even_hard_confirm_actions_outright(self):
        # Deny = refuse, logged — NOT a prompt: a denied app's
        # close action never reaches the confirmation card.
        policy = _policy(computer_use_app_rules=[
            {"app": "WINWORD.EXE", "verdict": "deny"}])
        close = classify_desktop_action(
            "desktop_act", {"action": "close_window", "app": "WINWORD.EXE"},
            resolved=_actuator().resolve_window_now(app="WINWORD.EXE"))
        decision = evaluate_desktop_action(policy, close, task_id="t1")
        assert decision.verdict == "deny"

    def test_grant_store_round_trip_and_revoke(self, tmp_path):
        store = DesktopGrantStore(tmp_path / "g.db")
        assert store.covers("WINWORD.EXE", "interact") is False
        assert store.grant("WINWORD.EXE", "interact") is True
        # Case-insensitive app matching; idempotent re-grant.
        assert store.covers("winword.exe", "interact") is True
        assert store.grant("WINWORD.EXE", "interact") is True
        grants = store.list_grants()
        assert len(grants) == 1
        assert store.revoke(grants[0]["id"]) is True
        assert store.covers("WINWORD.EXE", "interact") is False

    def test_grant_store_refuses_hard_confirm_categories(self, tmp_path):
        store = DesktopGrantStore(tmp_path / "g.db")
        assert store.grant("WINWORD.EXE", "close_or_quit") is False
        assert store.grant("WINWORD.EXE", "system") is False
        assert store.list_grants() == []

    def test_remembered_grant_allows_under_always_confirm(self):
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke", "element": {"name": "Save"}},
            resolved=_actuator().resolve_now({"name": "Save"}))
        decision = evaluate_desktop_action(
            _policy(), action, task_id="t1",
            grant_covers=lambda app, cat: (app, cat) == ("WINWORD.EXE", "interact"))
        assert (decision.verdict, decision.reason) == (
            "allow", "remembered_grant")


# --------------------------------------------------------------------
# Live control lane
# --------------------------------------------------------------------
class TestControlLane:
    def _save(self):
        return classify_desktop_action(
            "desktop_act", {"action": "invoke", "element": {"name": "Save"}},
            resolved=_actuator().resolve_now({"name": "Save"}))

    def test_pause_and_user_states_refuse(self):
        control = DesktopControl()
        policy = _policy(computer_use_category_modes=
                         {"interact": "allow_freely"})
        control.control("pause")
        decision = evaluate_desktop_action(
            policy, self._save(), task_id="t1",
            control_refusal=control.refusal_reason("t1"))
        assert (decision.verdict, decision.reason) == (
            "deny", "desktop_paused")
        control.control("take_control")
        decision = evaluate_desktop_action(
            policy, self._save(), task_id="t1",
            control_refusal=control.refusal_reason("t1"))
        assert (decision.verdict, decision.reason) == (
            "deny", "desktop_user_driving")
        control.control("return_control")
        decision = evaluate_desktop_action(
            policy, self._save(), task_id="t1",
            control_refusal=control.refusal_reason("t1"))
        assert decision.verdict == "allow"

    def test_stop_refuses_only_that_task(self):
        control = DesktopControl()
        policy = _policy(computer_use_category_modes=
                         {"interact": "allow_freely"})
        result = control.control("stop", task_id="t1")
        assert result["success"] is True
        assert control.refusal_reason("t1") == "desktop_stopped_for_task"
        # The task continues: another task is unaffected by the
        # stop, and t1's refusal is a refusal, not a kill.
        assert control.refusal_reason("t2") is None
        decision = evaluate_desktop_action(
            policy, self._save(), task_id="t2",
            control_refusal=control.refusal_reason("t2"))
        assert decision.verdict == "allow"

    def test_stop_defaults_to_the_driving_task(self):
        control = DesktopControl()
        control.note_driving_task("task-z")
        control.control("stop")
        assert control.refusal_reason("task-z") == "desktop_stopped_for_task"

    def test_unknown_control_action_is_refused(self):
        control = DesktopControl()
        result = control.control("explode")
        assert result["success"] is False
        assert control.state.value == "agent"

    def test_pixel_consent_is_per_session(self):
        control = DesktopControl()
        assert control.has_pixel_consent("s1") is False
        control.grant_pixel_consent("s1")
        assert control.has_pixel_consent("s1") is True
        assert control.has_pixel_consent("s2") is False


# --------------------------------------------------------------------
# Actuator contract (fake adapters)
# --------------------------------------------------------------------
class TestActuatorContract:
    def test_resolution_by_automation_id_and_name_role(self):
        actuator = _actuator()
        by_id = actuator.resolve_now({"automation_id": "btn-save"})
        assert by_id.element is not None
        assert by_id.element.name == "Save"
        by_name = actuator.resolve_now({"name": "Save", "role": "Button"})
        assert by_name.element is not None
        missing = actuator.resolve_now({"name": "Nope"})
        assert missing.element is None and missing.ambiguous is False

    def test_invoke_set_value_focus_close_clipboard(self):
        act = FakeActuationAdapter()
        actuator = _actuator(act=act)
        resolved = actuator.resolve_now({"name": "Save", "role": "Button"})
        ok, _detail = _run(actuator.invoke(resolved))
        assert ok is True
        assert act.calls[-1][0] == "invoke_element"
        assert act.calls[-1][2]["automation_id"] == "btn-save"

        doc = actuator.resolve_now({"name": "Document", "role": "Edit"})
        ok, _ = _run(actuator.set_value(doc, "Hello"))
        assert ok is True
        assert act.calls[-1][0] == "set_element_value"
        assert act.calls[-1][3] == "Hello"

        ok, _ = _run(actuator.focus_window(resolved))
        assert ok is True and act.calls[-1] == ("focus_window", 101)
        ok, _ = _run(actuator.close_window(resolved))
        assert ok is True and act.calls[-1] == ("close_window", 101)

        ok, text = _run(actuator.clipboard_get())
        assert (ok, text) == (True, "fake clipboard")
        ok, _ = _run(actuator.clipboard_set("new text"))
        assert ok is True and act.clipboard_text == "new text"

    def test_pixel_primitives(self):
        act = FakeActuationAdapter()
        actuator = _actuator(act=act)
        ok, _ = _run(actuator.mouse_click(320, 240))
        assert ok is True and act.calls[-1] == ("mouse_click", 320, 240)
        ok, _ = _run(actuator.type_text("abc"))
        assert ok is True and act.calls[-1] == ("type_text", "abc")
        ok, _ = _run(actuator.key_press("ctrl+s"))
        assert ok is True and act.calls[-1] == ("key_press", "ctrl+s")

    def test_element_at_point_picks_the_smallest_container(self):
        actuator = _actuator()
        hit = actuator.element_at_point_now(30, 25)
        assert hit is not None and hit.element.name == "Save"
        hit = actuator.element_at_point_now(500, 400)
        assert hit is not None and hit.element.name == "Document"
        # Off-desktop: a target over the window, with no element.
        assert actuator.element_at_point_now(5000, 5000).element is None

    def test_focused_element_resolution(self):
        actuator = _actuator()
        focused = actuator.focused_now()
        assert focused.element is not None
        assert focused.element.name == "Document"

    def test_unavailable_actuator_is_honest(self):
        actuator = DesktopActuator(UnavailableDesktopProvider("no backend"))
        available, reason = actuator.availability()
        assert available is False and "no backend" in reason
        ok, detail = _run(actuator.mouse_click(1, 1))
        assert ok is False and "no backend" in detail


# --------------------------------------------------------------------
# Vision grounding
# --------------------------------------------------------------------
def _tiny_png(width=4, height=3):
    row = b"\x10\x20\x30\xff" * width
    return _encode_png(width, height, [row] * height)


class _FakeCapture:
    def __init__(self, png):
        self._png = png

    def availability(self):
        return True, ""

    def capture_screen(self):
        return self._png


class _FakeVisionProvider:
    def __init__(self, reply):
        self._reply = reply
        self.last_messages = None

    async def call_stream(self, messages, tools=None, node_name=None,
                          **kwargs):
        self.last_messages = messages
        yield StreamChunk(text=self._reply, finish_reason="stop")


def _small_read():
    """A 40x30 fake desktop whose geometry lines up with a 40x30
    fake capture, so grounded image pixels are screen pixels."""
    tree = _element("Doc - Word", "Window", bounds=(0, 0, 40, 30),
                    children=[
                        _element("Save", "Button", can_invoke=True,
                                 bounds=(2, 2, 12, 10)),
                        _element("Close", "Button", can_invoke=True,
                                 bounds=(28, 2, 38, 10)),
                    ])
    return FakeReadAdapter(
        foreground={"title": "Doc - Word", "app": "WINWORD.EXE",
                    "handle": 7},
        windows=[{"title": "Doc - Word", "app": "WINWORD.EXE",
                  "handle": 7}],
        tree=tree)


def _small_png():
    row = b"\x10\x20\x30\xff" * 40
    return _encode_png(40, 30, [row] * 30)


def _small_actuator(act=None):
    return DesktopActuator(
        WindowsUiaProvider(_small_read()),
        act if act is not None else FakeActuationAdapter())


def _ground_on_small(reply, description="the Save button"):
    return _run(vision_module.ground_target(
        description,
        capture_provider=_FakeCapture(_small_png()),
        provider=_FakeVisionProvider(reply),
        actuator=_small_actuator()))


class TestVisionGrounding:
    def _ground(self, reply, **kwargs):
        return _run(vision_module.ground_target(
            "the Save button",
            capture_provider=_FakeCapture(_tiny_png()),
            provider=_FakeVisionProvider(reply), **kwargs))

    def test_coordinates_are_converted_and_used(self):
        result = self._ground('{"x": 500, "y": 500, "confidence": 0.95}')
        assert result.ok is True
        # 500/1000 across a 4x3 image maps to pixel (1, 1).
        assert (result.x, result.y) == (1, 1)
        assert result.confidence == 0.95

    def test_screenshot_is_never_persisted(self, monkeypatch):
        def _no_open(*args, **kwargs):
            raise AssertionError("grounding touched the filesystem")
        monkeypatch.setattr("builtins.open", _no_open)
        result = self._ground('{"x": 100, "y": 100, "confidence": 0.9}')
        assert result.ok is True

    def test_low_confidence_is_refused_without_a_uia_anchor(self):
        # On the small desktop, (1, 1) lands on the window root —
        # no element named like the target — so the low-confidence
        # hit is refused.
        result = _ground_on_small('{"x": 1, "y": 1, "confidence": 0.2}')
        assert result.ok is False
        assert "confidence" in result.reason

    def test_low_confidence_accepted_when_uia_verifies_the_target(self):
        # (175, 200) on the 40x30 small desktop maps to pixel
        # (6, 5), inside the Save button; the element name
        # matching the description anchors the low-confidence hit.
        result = _ground_on_small(
            '{"x": 175, "y": 200, "confidence": 0.3}')
        assert result.ok is True
        assert result.verified_element.element.name == "Save"

    def test_unconfigured_vision_model_is_honest(self):
        result = _run(vision_module.ground_target(
            "anything", capture_provider=_FakeCapture(_tiny_png()),
            provider=None))
        assert result.ok is False
        assert "vision model" in result.reason

    def test_unusable_reply_is_refused(self):
        result = self._ground("I cannot see any button")
        assert result.ok is False


# --------------------------------------------------------------------
# The desktop_act tool (fake actuator installed)
# --------------------------------------------------------------------
class TestDesktopActTool:
    """The tool returns the model-facing message string (the
    registry contract); status/category are asserted through the
    NODE_ACTIVITY events it publishes."""

    def _call(self, task_id="task-1", session_id="sess-1", **kwargs):
        async def _do():
            queue = asyncio.Queue()
            event_bus.subscribe(task_id, queue)
            try:
                with usage_context(session_id=session_id,
                                   task_id=task_id):
                    result = await _execute_desktop_act(**kwargs)
            finally:
                event_bus.unsubscribe(task_id, queue)
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return result, events
        return _run(_do())

    @staticmethod
    def _activity(events):
        from aether_common.contracts import EventType
        return [e.payload for e in events
                if e.type == EventType.NODE_ACTIVITY]

    def test_control_off_refuses_without_touching_the_actuator(
            self, desktop_env):
        desktop_env.holder["uc"] = UserConfig()
        result, _events = self._call(
            action="invoke", element={"name": "Save", "role": "Button"})
        assert "Desktop control is not enabled" in result
        assert desktop_env.act.calls == []

    def test_invoke_happy_path_publishes_activity(self, desktop_env):
        result, events = self._call(
            action="invoke", element={"name": "Save", "role": "Button"})
        assert result == "invoke_element done"
        assert desktop_env.act.calls[-1][0] == "invoke_element"
        activity = self._activity(events)
        assert activity, "expected a NODE_ACTIVITY event"
        payload = activity[-1]
        assert payload["action"] == "desktop_action"
        assert payload["input_payload"]["category"] == "interact"
        assert payload["output_payload"]["status"] == "completed"

    def test_ledger_records_the_action(self, desktop_env):
        from aether_engine import task_ledger
        task_ledger.clear_tap()
        _result, events = self._call(
            task_id="task-ledger-1",
            action="invoke", element={"name": "Save", "role": "Button"})
        for event in events:
            task_ledger.record_executor_event("task-ledger-1", event)
        entry = task_ledger.tap_snapshot("task-ledger-1")
        assert entry is not None
        assert any(
            a["action"] == "invoke" and a["category"] == "interact"
            for a in entry.get("desktop_actions", []))
        task_ledger.clear_tap()

    def test_stopped_task_refuses_and_the_lane_continues(
            self, desktop_env):
        control = get_desktop_control()
        control.control("stop", task_id="task-1")
        result, _events = self._call(
            action="invoke", element={"name": "Save", "role": "Button"})
        assert "The user stopped desktop control for this task" in result
        assert desktop_env.act.calls == []
        # The task itself is untouched — the stop is a lane state,
        # not a kill: a different task still acts.
        other, _events = self._call(
            task_id="task-2", session_id="sess-2",
            action="invoke", element={"name": "Save", "role": "Button"})
        assert other == "invoke_element done"

    def test_denied_app_refuses(self, desktop_env):
        desktop_env.holder["uc"] = _uc(computer_use_app_rules=[
            {"app": "WINWORD.EXE", "verdict": "deny"}])
        result, _events = self._call(
            action="invoke", element={"name": "Save", "role": "Button"})
        assert "The user has denied desktop control for WINWORD.EXE" in result
        assert desktop_env.act.calls == []

    def test_set_value_suggests_type_text_for_read_only_fields(
            self, desktop_env):
        # The Ribbon toolbar exposes no settable value pattern;
        # the tool must say so and name the honest alternative.
        result, _events = self._call(
            action="set_value",
            element={"name": "Ribbon", "role": "ToolBar"}, value="x")
        assert "does not accept a settable value" in result
        assert "type_text" in result

    def test_clipboard_round_trip_and_redaction(self, desktop_env):
        set_result, _events = self._call(
            action="clipboard_set", text="s3cret-clip")
        assert set_result == "clipboard_set done"
        get_result, events = self._call(action="clipboard_get")
        # The model receives the contents…
        assert "s3cret-clip" in get_result
        # …but the published activity (audit/activity records) only
        # ever carries the redacted detail.
        for payload in self._activity(events):
            assert "s3cret-clip" not in json.dumps(payload)
            assert payload["output_payload"]["detail"] == \
                "Read the clipboard (11 characters)."
        assert [c[0] for c in desktop_env.act.calls] == [
            "clipboard_set", "clipboard_get"]

    def _ground_setup(self, reply):
        act = FakeActuationAdapter()
        set_desktop_actuator(_small_actuator(act))
        capture_module.set_capture_provider(_FakeCapture(_small_png()))
        vision_module.set_vision_provider_resolver(
            lambda: _FakeVisionProvider(reply))
        return act

    def test_ground_click_clicks_a_verified_ordinary_target(
            self, desktop_env):
        act = self._ground_setup(
            '{"x": 175, "y": 200, "confidence": 0.9}')
        result, events = self._call(
            action="ground_click", description="the Save button")
        assert act.calls[-1] == ("mouse_click", 6, 5)
        assert "via vision grounding" in result
        activity = self._activity(events)
        assert activity[-1]["input_payload"]["via"] == "vision"

    def test_ground_click_refuses_a_hard_confirm_target(
            self, desktop_env):
        # Vision grounds the Close button at (33, 4); UIA
        # verification re-classifies it as close_or_quit and
        # execution refuses — grounding can never become a
        # confirmation bypass.
        act = self._ground_setup(
            '{"x": 850, "y": 150, "confidence": 0.9}')
        result, _events = self._call(
            action="ground_click", description="the Close button")
        assert "always need their own confirmation" in result
        assert act.calls == []


# --------------------------------------------------------------------
# The graph gate (pause node, mocked interrupt — browser pattern)
# --------------------------------------------------------------------
def _gate_state(args, task_id="task-1"):
    return {
        "pending_tool_call": {
            "name": "desktop_act", "args": args, "call_id": "call-1"},
        "task_id": task_id,
        "session_id": "sess-1",
        "messages": [],
    }


def _gate_config(uc, audit=None):
    return {"configurable": {
        "user_config": uc, "request_id": "req-1",
        "audit_logger": audit or _RecordingAudit(),
        "tool_registry": build_tool_registry(),
    }}


class TestGateIntegration:
    def test_tool_router_sends_desktop_writes_to_the_pause(self):
        state = {
            "pending_tool_call": {
                "name": "desktop_act",
                "args": {"action": "invoke",
                         "element": {"name": "Save"}},
                "call_id": "c1"},
            "task_id": "t", "session_id": "s",
            "approval_policy_override": "Auto",
        }
        assert tool_router(state, _gate_config(_uc())) == "pause_for_approval"

    def test_hard_confirm_pauses_even_with_allow_freely_everywhere(
            self, desktop_env, monkeypatch):
        uc = _uc(computer_use_category_modes={
            "interact": "allow_freely", "open": "allow_freely"})
        interrupt = Mock(return_value={"approved": True})
        import aether_engine.langgraph.nodes.pause as pause_module
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "close_window", "app": "WINWORD.EXE"}),
            _gate_config(uc))
        assert interrupt.call_count == 1
        payload = interrupt.call_args[0][0]
        confirmation = payload["desktop_confirmation"]
        assert confirmation["category"] == "close_or_quit"
        assert confirmation["hard_confirm"] is True
        assert result["pending_tool_call"] is not None

    def test_allow_freely_skips_the_pause_and_audits(
            self, desktop_env, monkeypatch):
        audit = _RecordingAudit()
        uc = _uc(computer_use_category_modes={"interact": "allow_freely"})
        import aether_engine.langgraph.nodes.pause as pause_module

        def _no_interrupt(_payload):
            raise AssertionError("allow_freely must not pause")
        monkeypatch.setattr(pause_module, "interrupt", _no_interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "invoke",
                         "element": {"name": "Save", "role": "Button"}}),
            _gate_config(uc, audit))
        assert result["pending_tool_call"]["approved"] is True
        assert result["pending_tool_call"]["desktop_authorization"][
            "basis"] == "policy"
        assert "DESKTOP_ACTION_AUTO_ALLOWED" in audit.actions()

    def test_denied_app_is_refused_in_place(self, desktop_env):
        uc = _uc(computer_use_app_rules=[
            {"app": "WINWORD.EXE", "verdict": "deny"}])
        result = dummy_pause_node(
            _gate_state({"action": "invoke",
                         "element": {"name": "Save", "role": "Button"}}),
            _gate_config(uc))
        assert result["pending_tool_call"] is None
        tool_msg = result["messages"][-1]
        assert "denied" in tool_msg["content"]

    def test_envelope_grant_lands_only_through_an_approval(
            self, desktop_env, monkeypatch):
        import aether_engine.langgraph.nodes.pause as pause_module
        interrupt = Mock(return_value={
            "approved": True, "desktop_envelope": {"grant": True}})
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        uc = _uc(computer_use_category_modes=
                 {"interact": "allow_with_envelope"})
        result = dummy_pause_node(
            _gate_state({"action": "invoke",
                         "element": {"name": "Save", "role": "Button"}}),
            _gate_config(uc, desktop_env.audit))
        assert result["pending_tool_call"]["approved"] is True
        # The envelope now covers the next identical action with
        # no further pause…
        store = desktop_policy.get_desktop_envelope_store()
        action = classify_desktop_action(
            "desktop_act", {"action": "invoke",
                            "element": {"name": "Save", "role": "Button"}},
            resolved=desktop_env.actuator.resolve_now(
                {"name": "Save", "role": "Button"}))
        decision = evaluate_desktop_action(
            policy_from_user_config(uc), action, task_id="task-1",
            envelope_store=store)
        assert decision.reason == "envelope"
        assert "DESKTOP_ENVELOPE_GRANTED" in desktop_env.audit.actions()

    def test_remember_grant_lands_through_an_approval(self, desktop_env,
                                                      monkeypatch):
        import aether_engine.langgraph.nodes.pause as pause_module
        audit = _RecordingAudit()
        interrupt = Mock(return_value={
            "approved": True, "desktop_remember": True})
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "invoke",
                         "element": {"name": "Save", "role": "Button"}}),
            _gate_config(_uc(), audit))
        assert result["pending_tool_call"]["approved"] is True
        assert desktop_env.grants.covers("WINWORD.EXE", "interact")
        assert "DESKTOP_GRANT_REMEMBERED" in audit.actions()

    def test_remember_is_refused_for_a_hard_confirm_action(
            self, desktop_env, monkeypatch):
        import aether_engine.langgraph.nodes.pause as pause_module
        interrupt = Mock(return_value={
            "approved": True, "desktop_remember": True})
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "close_window", "app": "WINWORD.EXE"}),
            _gate_config(_uc()))
        assert result["pending_tool_call"]["approved"] is True
        assert desktop_env.grants.list_grants() == []

    def test_pixel_consent_lands_through_an_approval(self, desktop_env,
                                                     monkeypatch):
        import aether_engine.langgraph.nodes.pause as pause_module
        audit = _RecordingAudit()
        uc = _uc(computer_use_pixel_fallback=True,
                 computer_use_category_modes={"interact": "allow_freely"})
        interrupt = Mock(return_value={"approved": True})
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "click_at", "x": 30, "y": 25}),
            _gate_config(uc, audit))
        assert result["pending_tool_call"]["approved"] is True
        assert get_desktop_control().has_pixel_consent("sess-1")
        assert "DESKTOP_PIXEL_CONSENT_GRANTED" in audit.actions()
        # With consent live, the next pixel action is auto-allowed
        # under allow_freely without a pause.
        def _no_interrupt(_payload):
            raise AssertionError("consented pixel must not pause")
        monkeypatch.setattr(pause_module, "interrupt", _no_interrupt)
        result = dummy_pause_node(
            _gate_state({"action": "click_at", "x": 30, "y": 25}),
            _gate_config(uc, audit))
        assert result["pending_tool_call"]["approved"] is True

    def test_pixel_fallback_off_denies_pixel_actions(self, desktop_env):
        result = dummy_pause_node(
            _gate_state({"action": "click_at", "x": 30, "y": 25}),
            _gate_config(_uc()))
        assert result["pending_tool_call"] is None
        assert "Pixel (mouse/keyboard) input is turned off" in \
            result["messages"][-1]["content"]


class TestFinalArgsGate:
    def _authorization(self, desktop_env, *, basis, policy_mode):
        # The record is built from a REAL classification so the
        # identity string is the engine's own, not a guess.
        action = classify_desktop_action(
            "desktop_act",
            {"action": "invoke",
             "element": {"name": "Save", "role": "Button"}},
            resolved=desktop_env.actuator.resolve_now(
                {"name": "Save", "role": "Button"}))
        return {
            "tool_name": "desktop_act", "category": action.category,
            "identity": action.identity, "basis": basis,
            "policy_mode": policy_mode,
        }

    def test_unchanged_action_proceeds(self, desktop_env):
        gate = make_desktop_final_args_gate(
            _gate_state({}), _gate_config(_uc()),
            self._authorization(desktop_env, basis="confirmed",
                                policy_mode="always_confirm"))
        assert _run(gate("desktop_act", {
            "action": "invoke",
            "element": {"name": "Save", "role": "Button"}})) is None

    def test_changed_target_reconfirms_and_denial_blocks(
            self, desktop_env, monkeypatch):
        import aether_engine.langgraph.nodes.pause as pause_module
        interrupt = Mock(return_value={"approved": False})
        monkeypatch.setattr(pause_module, "interrupt", interrupt)
        gate = make_desktop_final_args_gate(
            _gate_state({}), _gate_config(_uc()),
            self._authorization(desktop_env, basis="confirmed",
                                policy_mode="always_confirm"))
        from aether_engine.tools.registry import ToolExecutionError
        with pytest.raises(ToolExecutionError, match="declined"):
            _run(gate("desktop_act", {
                "action": "invoke", "element": {"name": "Close"}}))
        payload = interrupt.call_args[0][0]
        assert payload["desktop_reconfirmation"] is True

    def test_changed_target_under_freely_policy_proceeds(self, desktop_env):
        gate = make_desktop_final_args_gate(
            _gate_state({}),
            _gate_config(_uc(computer_use_category_modes=
                              {"interact": "allow_freely"})),
            self._authorization(desktop_env, basis="policy",
                                policy_mode="allow_freely"))
        # A different element, auto-allowed by the same free mode.
        assert _run(gate("desktop_act", {
            "action": "invoke",
            "element": {"name": "Document", "role": "Edit"}})) is None


# --------------------------------------------------------------------
# Offering: absent (and prompt byte-identical) unless permitted
# --------------------------------------------------------------------
class _CaptureProvider:
    def __init__(self):
        self.tools = None
        self.system_prompt = None

    async def call_stream(self, messages, tools=None, node_name=None,
                          **kwargs):
        self.tools = tools
        self.system_prompt = messages[0]["content"] if messages else ""
        yield StreamChunk(text="answer", finish_reason="stop")


def _runner_state():
    return {
        "original_prompt": "Do the task",
        "messages": [{"role": "user", "content": "Do the task"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": "researcher",
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "s20-runner",
        "session_id": "s20-runner-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }


def _tool_names(tools):
    return [(t.get("function") or {}).get("name") for t in (tools or [])]


class TestOffering:
    async def _run_runner(self, registry, uc):
        provider = _CaptureProvider()
        await run_specialist(
            RESEARCHER_SPEC, _runner_state(),
            {"configurable": {"provider": provider,
                              "tool_registry": registry,
                              "user_config": uc}},
            save_artifact=lambda *a, **k: "/tmp/x.txt")
        return provider

    async def test_offered_only_when_control_is_permitted(self):
        registry = build_tool_registry()
        off = await self._run_runner(registry, UserConfig())
        assert "desktop_act" not in _tool_names(off.tools)
        on = await self._run_runner(registry, _uc())
        assert "desktop_act" in _tool_names(on.tools)

    async def test_prompt_byte_identical_when_control_off(self):
        full = build_tool_registry()
        # A registry with the desktop tool removed entirely: with
        # control off, the offered surface AND the prompt must be
        # indistinguishable from this build-of-the-world.
        trimmed = ToolRegistry()
        for tool in full.get_tools():
            if tool.name != "desktop_act":
                trimmed.register(tool)
        with_tool = await self._run_runner(full, UserConfig())
        without_tool = await self._run_runner(trimmed, UserConfig())
        assert _tool_names(with_tool.tools) == _tool_names(without_tool.tools)
        assert with_tool.system_prompt == without_tool.system_prompt


# --------------------------------------------------------------------
# Config surface + acknowledgement + project-layer strip
# --------------------------------------------------------------------
class TestConfigSurface:
    def test_defaults_are_off(self):
        uc = UserConfig()
        assert uc.computer_use_control is False
        assert uc.computer_use_control_ack is False
        # S20 gate fix: the default is the policy's full default map
        # (every category always_confirm), not {} — from_dict cleans
        # through the same fill-defaults cleaner, so {} here would
        # make a fresh config unequal to its own save/load
        # round-trip (E6-F07's pin caught that at the full gate).
        from aether_engine.desktop.policy import default_category_modes
        assert uc.computer_use_category_modes == default_category_modes()
        assert uc.computer_use_app_rules == []
        assert uc.computer_use_envelopes_enabled is True
        assert uc.computer_use_pixel_fallback is False

    def test_control_permitted_truth_table(self):
        assert control_permitted(_uc()) is True
        assert control_permitted(_uc(computer_use_control_ack=False)) is False
        assert control_permitted(_uc(computer_use_control=False)) is False
        assert control_permitted(_uc(computer_use_enabled=False)) is False
        assert control_permitted(UserConfig()) is False

    def test_round_trip_preserves_control_fields(self):
        uc = _uc(computer_use_category_modes={"interact": "allow_freely"},
                 computer_use_app_rules=[
                     {"app": "WINWORD.EXE", "verdict": "deny"}],
                 computer_use_pixel_fallback=True)
        restored = UserConfig.from_dict(uc.to_dict())
        assert restored.computer_use_control is True
        assert restored.computer_use_control_ack is True
        # from_dict runs the modes through the policy cleaner,
        # which fills every unstated category with always_confirm.
        assert restored.computer_use_category_modes["interact"] == \
            "allow_freely"
        assert restored.computer_use_category_modes["system"] == \
            "always_confirm"
        assert restored.computer_use_app_rules == [
            {"app": "WINWORD.EXE", "verdict": "deny"}]
        assert restored.computer_use_pixel_fallback is True

    def test_validation_strictness(self):
        with pytest.raises(ValueError):
            validate_handed_off_updates({"computer_use_control": "yes"})
        with pytest.raises(ValueError):
            validate_handed_off_updates({"computer_use_control_ack": 1})
        with pytest.raises(ValueError, match="unknown category"):
            validate_handed_off_updates(
                {"computer_use_category_modes": {"dance": "allow_freely"}})
        with pytest.raises(ValueError, match="unknown mode"):
            validate_handed_off_updates(
                {"computer_use_category_modes": {"interact": "yolo"}})
        with pytest.raises(ValueError, match="verdict"):
            validate_handed_off_updates(
                {"computer_use_app_rules":
                 [{"app": "X.EXE", "verdict": "maybe"}]})
        cleaned = validate_handed_off_updates(
            {"computer_use_app_rules":
             [{"app": " X.EXE ", "verdict": "deny"}]})
        assert cleaned["computer_use_app_rules"] == [
            {"app": "X.EXE", "verdict": "deny"}]

    def test_project_layer_can_never_carry_control_fields(self, tmp_path):
        from aether_engine.config_layers import effective_user_config
        project_dir = tmp_path / "proj"
        (project_dir / ".aether").mkdir(parents=True)
        (project_dir / ".aether" / "config.json").write_text(json.dumps({
            "computer_use_enabled": True,
            "computer_use_control": True,
            "computer_use_control_ack": True,
            "computer_use_pixel_fallback": True,
            "computer_use_category_modes": {"interact": "allow_freely"},
        }))
        effective = effective_user_config(
            UserConfig(), workspace_root=str(project_dir))
        assert effective.computer_use_enabled is False
        assert effective.computer_use_control is False
        assert effective.computer_use_control_ack is False
        assert effective.computer_use_pixel_fallback is False
        # The layer's allow_freely attempt never lands: the merged
        # modes are the global (default, all-confirm) ones.
        assert effective.computer_use_category_modes["interact"] == \
            "always_confirm"


# --------------------------------------------------------------------
# WebSocket handlers: control lane, grants, ack refusal
# --------------------------------------------------------------------
class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _ws_conn():
    from aether_engine.routes import websocket as ws_module
    from aether_engine.routes import ws_connection
    return ws_module, ws_connection.WsConnection(
        _FakeSocket(), object(), object())


def _event(event_type, payload=None, req_id="req-1"):
    from aether_common.contracts import Event
    return Event(type=event_type, request_id=req_id,
                 payload=payload or {})


class TestWsHandlers:
    def test_control_request_moves_the_lane(self, desktop_env):
        from aether_common.contracts import EventType as EventTypes
        ws_module, conn = _ws_conn()
        _run(ws_module._handle_desktop_control_request(
            conn, _event(EventTypes.DESKTOP_CONTROL_REQUEST,
                         {"action": "pause"})))
        response = conn.ws.sent[-1]
        assert response["type"] == "DESKTOP_CONTROL_RESPONSE"
        assert response["payload"]["success"] is True
        assert response["payload"]["control_state"] == "paused"
        assert get_desktop_control().state.value == "paused"

    def test_grants_request_lists_and_revokes(self, desktop_env):
        from aether_common.contracts import EventType as EventTypes
        ws_module, conn = _ws_conn()
        desktop_env.grants.grant("WINWORD.EXE", "interact")
        _run(ws_module._handle_desktop_grants_request(
            conn, _event(EventTypes.DESKTOP_GRANTS_REQUEST,
                         {"action": "list"})))
        grants = conn.ws.sent[-1]["payload"]["grants"]
        assert len(grants) == 1
        _run(ws_module._handle_desktop_grants_request(
            conn, _event(EventTypes.DESKTOP_GRANTS_REQUEST,
                         {"action": "revoke",
                          "grant_id": grants[0]["id"]})))
        assert conn.ws.sent[-1]["payload"]["success"] is True
        assert desktop_env.grants.list_grants() == []

    def test_config_update_refuses_control_without_ack(self, monkeypatch):
        from aether_common.contracts import EventType as EventTypes
        from aether_engine.routes import websocket as ws_module
        from aether_engine.routes import ws_connection
        from aether_engine.state import engine_state
        monkeypatch.setattr(engine_state, "user_config", UserConfig())
        monkeypatch.setattr(ws_module, "save_config", lambda cfg: None)
        conn = ws_connection.WsConnection(_FakeSocket(), object(), object())
        _run(ws_module._handle_config_update_request(
            conn, _event(EventTypes.CONFIG_UPDATE_REQUEST,
                         {"config": {"computer_use_control": True}})))
        response = conn.ws.sent[-1]
        assert response["payload"]["success"] is False
        assert "acknowledgement" in response["payload"]["error"]
        assert engine_state.user_config.computer_use_control is False
        # With the acknowledgement stored, the same update lands.
        monkeypatch.setattr(
            engine_state, "user_config",
            UserConfig(computer_use_control_ack=True))
        _run(ws_module._handle_config_update_request(
            conn, _event(EventTypes.CONFIG_UPDATE_REQUEST,
                         {"config": {"computer_use_enabled": True,
                                     "computer_use_control": True}})))
        assert conn.ws.sent[-1]["payload"]["success"] is True
        assert engine_state.user_config.computer_use_control is True
