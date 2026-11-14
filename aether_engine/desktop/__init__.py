"""Desktop Computer Use: observe (S19) + actuation (S20).

PRIVACY SHAPE — the contract of this package:

- Observe reads accessibility text only — the names, roles
  and values the operating system exposes through UI
  Automation — for the FOREGROUND window, plus the titles of
  the other visible windows.
- A read happens only when a tool calls for it during a task
  the user started. Nothing is captured continuously, nothing
  is watched in the background, and observe results are not
  stored beyond the turn's prompt.
- Actuation (S20, owner decision O1) is policy-gated end to
  end: UIA by element first, vision grounding only for
  UIA-blind targets (a screenshot taken for ONE grounding
  call, discarded after it — ``desktop/vision.py``), raw pixel
  input as its own switchable, consent-gated class. Closing /
  quitting and system actions always confirm individually.
  The gates: :func:`observe_permitted` for reads and
  :func:`control_permitted` for actuation — master switch AND
  Control AND the one-time acknowledgement.

The public surface: :func:`get_desktop_provider`,
:func:`get_desktop_actuator`, the two config gates, and the
provider module's data shapes.
"""
from __future__ import annotations

import platform
from typing import Any

from aether_engine.desktop.provider import (
    MAX_TREE_DEPTH,
    MAX_TREE_ELEMENTS,
    SCREEN_STATE_MAX_CHARS,
    DesktopActuator,
    DesktopProvider,
    ElementNode,
    ResolvedTarget,
    ScreenState,
    UnavailableDesktopProvider,
    WindowInfo,
    WindowsUiaProvider,
    summarise_screen_state,
)

__all__ = [
    "MAX_TREE_DEPTH",
    "MAX_TREE_ELEMENTS",
    "SCREEN_STATE_MAX_CHARS",
    "DesktopActuator",
    "DesktopProvider",
    "ElementNode",
    "ResolvedTarget",
    "ScreenState",
    "UnavailableDesktopProvider",
    "WindowInfo",
    "WindowsUiaProvider",
    "control_permitted",
    "get_desktop_actuator",
    "get_desktop_provider",
    "observe_permitted",
    "summarise_screen_state",
]


def get_desktop_provider() -> DesktopProvider:
    """The provider for this machine: the Windows UIA provider
    on Windows (its availability probe reports the honest
    reason when the UIA package is missing), and the
    unavailable provider elsewhere — desktop observe is built
    on Windows UI Automation, and other platforms say so
    rather than failing at read time."""
    system = platform.system()
    if system == "Windows":
        return WindowsUiaProvider()
    return UnavailableDesktopProvider(
        "Desktop observe is built on Windows UI Automation; "
        f"{system or 'this platform'} is not a supported "
        "platform for it.")


def observe_permitted(user_config: Any) -> bool:
    """The one config gate for desktop observe: the Computer
    Use master switch AND the Observe switch must both be on.
    Every consumer — the supervisor's tool offering, the tool
    gate at call time, the routing prompt's tool section —
    reads this, so the surfaces can never disagree about
    whether the tool exists."""
    if user_config is None:
        return False
    return bool(getattr(user_config, "computer_use_enabled", False)) \
        and bool(getattr(user_config, "computer_use_observe", False))


def control_permitted(user_config: Any) -> bool:
    """The one config gate for desktop actuation (S20): master
    switch AND Control AND the one-time first-enable
    acknowledgement. The runner's tool offering, the policy
    gate, and the tool's own execution check all read this (via
    the policy value object, which resolves the same three
    fields), so the surfaces can never disagree about whether
    actuation exists. A hand-edited config cannot skip the ack:
    the flag is part of the gate, not just panel sequencing."""
    if user_config is None:
        return False
    return bool(getattr(user_config, "computer_use_enabled", False)) \
        and bool(getattr(user_config, "computer_use_control", False)) \
        and bool(getattr(user_config, "computer_use_control_ack", False))


_actuator: DesktopActuator | None = None


def get_desktop_actuator() -> DesktopActuator:
    """The process-wide actuator: the platform provider's read
    side for resolution plus the platform actuation backend
    (built lazily inside the actuator on first action)."""
    global _actuator
    if _actuator is None:
        _actuator = DesktopActuator(get_desktop_provider())
    return _actuator


def set_desktop_actuator(actuator: DesktopActuator | None) -> None:
    """Replace the process-wide actuator (tests)."""
    global _actuator
    _actuator = actuator
