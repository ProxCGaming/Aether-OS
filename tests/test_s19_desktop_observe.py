"""Master build S19: desktop observe — UIA read-only + the
Computer Use config gates.

The audit (desktop G3/G4, plan R10) found no UIA at all: the
decided-primary route for seeing other apps lived only in the
avatar spec. S19 builds the OBSERVE half only — reading the
foreground window's accessibility tree as text — behind the
owner's decided control surface (master switch, separate
Observe switch, vision capture mode stored for S20, no
actuation anywhere).

These tests pin:

- the provider contract against a FAKE raw adapter (the real
  Windows backend cannot run in this environment): tree
  distillation caps (depth, element count, summary chars),
  focus marking, unnamed-leaf pruning, foreground exclusion
  from the window list;
- the REAL provider's honest unavailable on this Linux VM,
  and the hung-read timeout degrading instead of hanging;
- the config surface: defaults off, round-trip, strict
  handed-off validation (real booleans, closed vision-mode
  set naming the valid values);
- the supervisor tool's gates: not offered and not in the
  prompt with Computer Use off (byte-identical), offered when
  both switches are on, refused at call time when off, and
  refused under a per-tool Deny even when on;
- the tool's READ_ONLY policy classification.
"""
import asyncio
import time

import pytest

from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.desktop import (
    MAX_TREE_ELEMENTS,
    SCREEN_STATE_MAX_CHARS,
    WindowsUiaProvider,
    get_desktop_provider,
    observe_permitted,
    summarise_screen_state,
)
from aether_engine.desktop.provider import ScreenState
from aether_engine.tools.policy import (
    resolve_policy_by_name,
    tool_risk_by_name,
)
from aether_engine.tools.registry import ToolRisk


# --------------------------------------------------------------------
# Fake raw adapter + fixtures
# --------------------------------------------------------------------
def _element(name="", role="Text", value=None, focused=False, children=(),
             automation_id=None):
    return {
        "name": name, "role": role, "value": value,
        "automation_id": automation_id, "focused": focused,
        "bounds": (0, 0, 100, 20), "children": list(children),
    }


class FakeAdapter:
    """A RawDesktopAdapter over fixed raw dicts — the provider
    contract is exercised through this, never through the OS."""

    def __init__(self, foreground=None, windows=(), tree=None, delay=0.0):
        self._foreground = foreground
        self._windows = list(windows)
        self._tree = tree
        self._delay = delay

    def foreground_window(self):
        if self._delay:
            time.sleep(self._delay)
        return self._foreground

    def list_windows(self):
        return list(self._windows)

    def read_tree(self, handle, *, max_depth, max_elements):
        return self._tree


def _word_adapter():
    tree = _element("Document1 - Word", "Window", children=[
        _element("Ribbon", "ToolBar", children=[
            _element("Save", "Button"),
            _element("", "Separator"),  # unnamed leaf: pruned
        ]),
        _element("Document", "Edit", value="Dear team,",
                 focused=True),
        _element("Status Bar", "StatusBar", children=[
            _element("Page 1 of 1", "Text"),
        ]),
    ])
    return FakeAdapter(
        foreground={"title": "Document1 - Word", "app": "WINWORD.EXE",
                    "handle": 101},
        windows=[
            {"title": "Document1 - Word", "app": "WINWORD.EXE",
             "handle": 101},
            {"title": "Inbox - Outlook", "app": "OUTLOOK.EXE",
             "handle": 102},
            {"title": "Terminal", "app": "wt.exe", "handle": 103},
        ],
        tree=tree,
    )


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------
# Provider contract (fake adapter)
# --------------------------------------------------------------------
class TestProviderContract:
    def test_foreground_windows_and_tree(self):
        provider = WindowsUiaProvider(_word_adapter())
        assert provider.availability() == (True, "")
        state = _run(provider.screen_state())
        assert state.available
        assert state.foreground.title == "Document1 - Word"
        assert state.foreground.app == "WINWORD.EXE"
        # The foreground window is excluded from the others list.
        assert [w.title for w in state.windows] == [
            "Inbox - Outlook", "Terminal"]
        assert state.root is not None
        roles = [c.role for c in state.root.children]
        assert roles == ["ToolBar", "Edit", "StatusBar"]

    def test_summary_renders_focus_value_and_prunes(self):
        provider = WindowsUiaProvider(_word_adapter())
        summary = summarise_screen_state(_run(provider.screen_state()))
        assert 'Foreground window: "Document1 - Word" (WINWORD.EXE)' in summary
        assert 'Other visible windows (2)' in summary
        assert '- Edit "Document" value="Dear team," [focused]' in summary
        assert "Separator" not in summary  # unnamed leaf pruned
        assert "truncated" not in summary

    def test_depth_cap(self):
        node = _element("leaf", "Text")
        for depth in range(12):
            node = _element(f"d{depth}", "Pane", children=[node])
        adapter = FakeAdapter(
            foreground={"title": "Deep", "app": "deep.exe", "handle": 1},
            windows=[], tree=node)
        state = _run(WindowsUiaProvider(adapter).screen_state())

        def deepest(n, d=0):
            if not n.children:
                return d
            return deepest(n.children[0], d + 1)

        from aether_engine.desktop import MAX_TREE_DEPTH

        assert deepest(state.root) == MAX_TREE_DEPTH

    def test_element_cap_marks_truncation(self):
        children = [_element(f"item {i}", "ListItem") for i in range(400)]
        tree = _element("Big", "Window", children=children)
        adapter = FakeAdapter(
            foreground={"title": "Big", "app": "big.exe", "handle": 1},
            windows=[], tree=tree)
        state = _run(WindowsUiaProvider(adapter).screen_state())
        assert state.tree_truncated
        summary = summarise_screen_state(state)
        assert f"truncated at {MAX_TREE_ELEMENTS} elements" in summary

    def test_summary_char_cap(self):
        children = [
            _element("x" * 500, "Text", value="y" * 500)
            for _ in range(100)
        ]
        tree = _element("Wide", "Window", children=children)
        adapter = FakeAdapter(
            foreground={"title": "Wide", "app": "w.exe", "handle": 1},
            windows=[], tree=tree)
        summary = summarise_screen_state(
            _run(WindowsUiaProvider(adapter).screen_state()))
        assert len(summary) <= SCREEN_STATE_MAX_CHARS + 60
        assert "screen summary truncated to budget" in summary

    def test_no_foreground_window(self):
        adapter = FakeAdapter(foreground=None, windows=[], tree=None)
        state = _run(WindowsUiaProvider(adapter).screen_state())
        assert state.available
        assert state.foreground is None
        summary = summarise_screen_state(state)
        assert "Foreground window: none reported." in summary

    def test_hung_read_times_out_instead_of_hanging(self):
        # The contract: screen_state RETURNS at the timeout
        # with the honest unavailable — the engine's long-lived
        # loop never waits on the stuck OS thread (it finishes
        # in the background, discarded). Measured via a daemon
        # thread + queue because asyncio.run's own shutdown
        # joins executor threads, which would hide exactly the
        # promptness being pinned.
        import queue
        import threading

        adapter = FakeAdapter(
            foreground={"title": "Slow", "app": "s.exe", "handle": 1},
            windows=[], tree=None, delay=2.0)
        provider = WindowsUiaProvider(adapter, timeout_s=0.1)
        results: queue.Queue = queue.Queue()

        def _drive():
            # A bare loop, deliberately not asyncio.run: its
            # shutdown joins executor threads (the 2 s sleep),
            # which is runner bookkeeping, not the read's wait.
            loop = asyncio.new_event_loop()
            try:
                results.put(loop.run_until_complete(
                    provider.screen_state()))
            finally:
                loop.close()

        start = time.monotonic()
        threading.Thread(target=_drive, daemon=True).start()
        state = results.get(timeout=1.5)
        assert time.monotonic() - start < 1.5
        assert not state.available
        assert "did not answer" in state.reason

    def test_adapter_failure_is_an_honest_unavailable(self):
        class BoomAdapter(FakeAdapter):
            def foreground_window(self):
                raise RuntimeError("COM died")

        state = _run(WindowsUiaProvider(BoomAdapter()).screen_state())
        assert not state.available
        assert "The desktop read failed" in state.reason


class TestRealProviderHere:
    def test_real_provider_is_honestly_unavailable_on_this_vm(self):
        # This VM is Linux: the factory's provider must say so,
        # with the platform reason — not raise, not fake data.
        provider = get_desktop_provider()
        available, reason = provider.availability()
        assert not available
        assert "Windows UI Automation" in reason
        state = _run(provider.screen_state())
        assert not state.available
        assert state.reason == reason
        assert state.root is None and state.foreground is None

    def test_unavailable_screen_state_summary_is_not_rendered(self):
        # The unavailable shape carries no data to summarise;
        # the tool phrases it from the reason instead.
        state = ScreenState.unavailable("some reason")
        assert not state.available and state.reason == "some reason"


# --------------------------------------------------------------------
# Config surface
# --------------------------------------------------------------------
class TestConfig:
    def test_defaults_are_off(self):
        config = UserConfig()
        assert config.computer_use_enabled is False
        assert config.computer_use_observe is False
        assert config.computer_use_vision_capture == "off"

    def test_round_trip(self):
        config = UserConfig(
            computer_use_enabled=True, computer_use_observe=True,
            computer_use_vision_capture="on_demand")
        restored = UserConfig.from_dict(config.to_dict())
        assert restored.computer_use_enabled is True
        assert restored.computer_use_observe is True
        assert restored.computer_use_vision_capture == "on_demand"

    def test_from_dict_garbage_falls_back_to_off(self):
        restored = UserConfig.from_dict(
            {"computer_use_vision_capture": "always_on"})
        assert restored.computer_use_vision_capture == "off"

    def test_observe_permitted_needs_both_switches(self):
        assert not observe_permitted(None)
        assert not observe_permitted(UserConfig())
        assert not observe_permitted(
            UserConfig(computer_use_enabled=True))
        assert not observe_permitted(
            UserConfig(computer_use_observe=True))
        assert observe_permitted(UserConfig(
            computer_use_enabled=True, computer_use_observe=True))

    def test_validation_accepts_the_three_fields(self):
        cleaned = validate_handed_off_updates({
            "computer_use_enabled": True,
            "computer_use_observe": False,
            "computer_use_vision_capture": "Ask",
        })
        assert cleaned == {
            "computer_use_enabled": True,
            "computer_use_observe": False,
            "computer_use_vision_capture": "ask",
        }

    @pytest.mark.parametrize("field", ["computer_use_enabled",
                                       "computer_use_observe"])
    def test_validation_refuses_non_bool_switches(self, field):
        with pytest.raises(ValueError, match="must be true or false"):
            validate_handed_off_updates({field: "yes"})

    def test_validation_refuses_unknown_vision_mode(self):
        with pytest.raises(ValueError, match="off, ask, on_demand"):
            validate_handed_off_updates(
                {"computer_use_vision_capture": "always_on"})


# --------------------------------------------------------------------
# Policy classification
# --------------------------------------------------------------------
class TestPolicyClassification:
    def test_get_screen_state_is_read_only(self):
        assert tool_risk_by_name("get_screen_state", None) is ToolRisk.READ_ONLY

    def test_default_verdict_is_allow_and_deny_is_honoured(self):
        assert resolve_policy_by_name("get_screen_state", {}) == "allow"
        assert resolve_policy_by_name(
            "get_screen_state", {"get_screen_state": "Deny"}) == "deny"


# --------------------------------------------------------------------
# Supervisor offering + gates
# --------------------------------------------------------------------
def _offered_names(user_config):
    from aether_engine.langgraph.nodes.supervisor import (
        build_supervisor_desktop_tools,
        build_supervisor_memory_tools,
    )
    from aether_engine.langgraph.supervisor_model import (
        AetherSupervisorModel,
    )

    model = AetherSupervisorModel()
    model.aether_tools = (
        build_supervisor_memory_tools() + build_supervisor_desktop_tools())
    config = {"configurable": {
        "user_config": user_config, "agent_tools": {}}}
    offered = model._offered_tools(config, [], ["researcher"])
    return [t["function"]["name"] for t in offered]


def _routing_prompt_text(monkeypatch, user_config):
    from aether_engine.langgraph import supervisor_model

    monkeypatch.setattr(
        supervisor_model, "get_config",
        lambda: {"configurable": {"user_config": user_config}})
    messages = supervisor_model.supervisor_routing_prompt(
        {"original_prompt": "what is on my screen?",
         "plan": "No plan yet.", "messages": []})
    return messages[0].content


class TestSupervisorOffering:
    def test_tool_absent_when_computer_use_off(self):
        names = _offered_names(UserConfig())
        assert "get_screen_state" not in names
        # …and the memory tools are offered exactly as before.
        assert "get_user_profile" in names

    def test_tool_absent_when_only_one_switch_on(self):
        assert "get_screen_state" not in _offered_names(
            UserConfig(computer_use_enabled=True))
        assert "get_screen_state" not in _offered_names(
            UserConfig(computer_use_observe=True))

    def test_tool_offered_when_both_switches_on(self):
        names = _offered_names(UserConfig(
            computer_use_enabled=True, computer_use_observe=True))
        assert "get_screen_state" in names

    def test_prompt_byte_identical_when_off(self, monkeypatch):
        # No user_config at all and an explicit default config
        # (both switches off) produce the identical prompt, and
        # neither mentions the tool — pre-S19 behaviour pinned.
        bare = _routing_prompt_text(monkeypatch, None)
        defaulted = _routing_prompt_text(monkeypatch, UserConfig())
        assert bare == defaulted
        assert "get_screen_state" not in bare

    def test_prompt_gains_the_tool_section_when_on(self, monkeypatch):
        text = _routing_prompt_text(monkeypatch, UserConfig(
            computer_use_enabled=True, computer_use_observe=True))
        assert "get_screen_state" in text
        assert "cannot click, type, or take screenshots" in text


class TestCallTimeGate:
    def _gate(self, user_config, agent_tools=None):
        from aether_engine.langgraph.nodes.supervisor import (
            _desktop_tool_gate,
        )

        config = {"configurable": {
            "user_config": user_config,
            "agent_tools": agent_tools or {}}}
        return _desktop_tool_gate("get_screen_state", config)

    def test_refused_when_off(self):
        gate = self._gate(UserConfig())
        assert gate is not None and "Computer Use" in gate

    def test_allowed_when_on(self):
        assert self._gate(UserConfig(
            computer_use_enabled=True,
            computer_use_observe=True)) is None

    def test_policy_deny_refuses_even_when_on(self):
        config = UserConfig(
            computer_use_enabled=True, computer_use_observe=True)
        config.tool_policies = {"get_screen_state": "Deny"}
        gate = self._gate(config)
        assert gate is not None and "Deny" in gate

    def test_allow_list_refuses_even_when_on(self):
        gate = self._gate(
            UserConfig(computer_use_enabled=True,
                       computer_use_observe=True),
            agent_tools={"supervisor": ["search_knowledge_graph"]})
        assert gate is not None and "disabled for your agent role" in gate


class TestToolExecution:
    def test_execution_renders_the_fake_desktop(self, monkeypatch):
        from aether_engine.langgraph.nodes import supervisor

        provider = WindowsUiaProvider(_word_adapter())
        # _execute_supervisor_screen_tool imports the factory
        # from the desktop package at call time; patch it there.
        import aether_engine.desktop as desktop_pkg

        monkeypatch.setattr(
            desktop_pkg, "get_desktop_provider", lambda: provider)
        result = _run(supervisor._execute_supervisor_screen_tool(
            "", user_config=UserConfig(
                computer_use_enabled=True, computer_use_observe=True)))
        assert 'Foreground window: "Document1 - Word"' in result
        assert "[focused]" in result

    def test_execution_reports_unavailable_honestly(self):
        from aether_engine.langgraph.nodes import supervisor

        # No patching: on this Linux VM the real provider is
        # unavailable, and the tool must say so, not raise.
        result = _run(supervisor._execute_supervisor_screen_tool(""))
        assert result.startswith("Desktop observe is unavailable:")
        assert "Windows UI Automation" in result

    def test_execution_result_is_capped(self, monkeypatch):
        import aether_engine.desktop as desktop_pkg
        from aether_engine.langgraph.nodes import supervisor

        children = [_element("x" * 500, "Text", value="y" * 500)
                    for _ in range(100)]
        adapter = FakeAdapter(
            foreground={"title": "Wide", "app": "w.exe", "handle": 1},
            windows=[], tree=_element("Wide", "Window", children=children))
        provider = WindowsUiaProvider(adapter)
        monkeypatch.setattr(
            desktop_pkg, "get_desktop_provider", lambda: provider)
        result = _run(supervisor._execute_supervisor_screen_tool(""))
        assert len(result) <= SCREEN_STATE_MAX_CHARS + 60
