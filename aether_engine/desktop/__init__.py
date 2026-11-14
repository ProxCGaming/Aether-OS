"""Desktop observe (master build S19): Aether reading the
user's screen as accessibility TEXT, on request only.

PRIVACY SHAPE — the contract of this package:

- Observe reads accessibility text only — the names, roles
  and values the operating system exposes through UI
  Automation — for the FOREGROUND window, plus the titles of
  the other visible windows. It takes no screenshots and
  reads no pixels; vision capture is a separate, later
  feature (S20) behind its own setting.
- A read happens only when the supervisor calls the
  ``get_screen_state`` tool during a task the user started.
  Nothing is captured continuously, nothing is watched in the
  background, and nothing is stored beyond the turn's prompt.
- Observe is read-only. Actuation (clicking, typing,
  invoking) is S20, gated on the owner's separate decision;
  there is no actuation code in this package.

The public surface: :func:`get_desktop_provider` (the
platform-selected provider), :func:`observe_permitted` (the
config gate every consumer shares — master switch AND
observe switch), and the provider module's data shapes.
"""
from __future__ import annotations

import platform
from typing import Any

from aether_engine.desktop.provider import (
    MAX_TREE_DEPTH,
    MAX_TREE_ELEMENTS,
    SCREEN_STATE_MAX_CHARS,
    DesktopProvider,
    ElementNode,
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
    "DesktopProvider",
    "ElementNode",
    "ScreenState",
    "UnavailableDesktopProvider",
    "WindowInfo",
    "WindowsUiaProvider",
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
