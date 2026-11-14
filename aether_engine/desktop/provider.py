"""Desktop observe: the provider seam and the tree distillation.

Master build S19. This module is the platform-neutral half of
Computer Use *observe*: the data shapes a screen-state read
produces, the raw-adapter protocol the OS backends implement,
the distillation that turns a raw accessibility tree into a
bounded text summary, and the two providers (the Windows UIA
provider and the honest unavailable provider).

PRIVACY SHAPE — the contract this whole package keeps:

- Observe reads accessibility TEXT only: the names, roles and
  values the operating system exposes through UI Automation.
  It never takes a screenshot (vision capture is S20's, behind
  its own setting) and never reads pixels.
- Only the FOREGROUND window's element tree is read, plus the
  titles of the other visible top-level windows.
- A read happens only when the supervisor calls the
  ``get_screen_state`` tool during a task the user started —
  never proactively, never on a timer, never in the background.
  Nothing is captured continuously and nothing is stored: the
  summary lives only in that turn's prompt.
- This slice is READ-ONLY. There is no invoke, click, type or
  any other actuation anywhere in the package; actuation is
  S20, gated on the owner's O1 decision.

The split mirrors ``memory/backend_status.py`` (S9) and
``machine_capability.py`` (S10): availability is probed WITHOUT
doing the work (``find_spec`` locates the UIA package without
importing it), an unavailable backend says so with its reason
instead of failing at call time, and the OS-specific half lives
behind a thin adapter protocol (``windows_uia.py``) so the
provider contract — caps, focus marking, timeout — is testable
against a fake adapter on any platform.
"""
from __future__ import annotations

import asyncio
import platform
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Bounds. A foreground window's accessibility tree can hold
# thousands of elements (a browser window, an IDE); the summary
# is prompt material, so every axis is capped and the cap is
# reported in the summary itself when it bites — a truncated
# tree is honest, a silently partial one is not.
# ---------------------------------------------------------------------------
MAX_TREE_DEPTH = 5            # the window itself is depth 0
MAX_TREE_ELEMENTS = 150       # converted elements, window included
MAX_WINDOWS_LISTED = 25       # other visible top-level windows
MAX_FIELD_CHARS = 100         # per element name / value in the summary
SCREEN_STATE_MAX_CHARS = 4000  # the whole summary, hard cap
READ_TIMEOUT_SECONDS = 5.0    # a hung UIA read degrades, never hangs


# ---------------------------------------------------------------------------
# Data shapes.
# ---------------------------------------------------------------------------
@dataclass
class WindowInfo:
    """One top-level window: its title and owning app (process
    name), as the OS reports them. ``handle`` is the backend's
    opaque window id (an HWND on Windows) — carried so a later
    slice can address the window, never rendered."""
    title: str
    app: str
    handle: int | None = None


@dataclass
class ElementNode:
    """One accessibility element, distilled. ``bounds`` is the
    element's bounding rectangle (left, top, right, bottom in
    screen pixels) when the backend reports one — the avatar
    spec's UIA route is rectangle-based, so the data is kept;
    the text summary does not print it."""
    name: str = ""
    role: str = ""
    value: str | None = None
    automation_id: str | None = None
    focused: bool = False
    bounds: tuple[int, int, int, int] | None = None
    children: list[ElementNode] = field(default_factory=list)


@dataclass
class ScreenState:
    """The result of one screen-state read. ``available`` False
    carries the honest ``reason`` and no partial data — a read
    that could not happen is reported, never faked."""
    available: bool
    reason: str = ""
    foreground: WindowInfo | None = None
    # The OTHER visible top-level windows — the provider
    # excludes the foreground window from this list.
    windows: list[WindowInfo] = field(default_factory=list)
    root: ElementNode | None = None
    tree_truncated: bool = False

    @classmethod
    def unavailable(cls, reason: str) -> ScreenState:
        return cls(available=False, reason=reason)


# ---------------------------------------------------------------------------
# The raw adapter protocol: the ONLY thing an OS backend must
# provide. It speaks plain dicts so the backend stays a thin
# translation of its OS API and every conversion rule lives
# here, testable without the OS.
#
#   window dict:  {"title": str, "app": str, "handle": int|None}
#   element dict: {"name": str, "role": str, "value": str|None,
#                  "automation_id": str|None, "focused": bool,
#                  "bounds": (l, t, r, b)|None,
#                  "children": [element dict, ...]}
# ---------------------------------------------------------------------------
@runtime_checkable
class RawDesktopAdapter(Protocol):
    """Synchronous, blocking OS reads. Implementations run on a
    worker thread (see WindowsUiaProvider.screen_state) — they
    may block, and the provider's timeout bounds how long the
    engine waits for them."""

    def foreground_window(self) -> dict[str, Any] | None:
        """The foreground window dict, or None when there is no
        foreground window (locked screen, secure desktop)."""
        ...

    def list_windows(self) -> list[dict[str, Any]]:
        """All visible top-level windows, foreground included."""
        ...

    def read_tree(self, handle: int | None, *, max_depth: int,
                  max_elements: int) -> dict[str, Any] | None:
        """The foreground window's element tree as nested dicts,
        walked no deeper than ``max_depth`` and no wider than
        ``max_elements`` — the caps bind the OS walk itself, so
        a huge tree never fully materialises."""
        ...


class DesktopProvider(Protocol):
    """What the supervisor tool consumes: an availability probe
    that never does the work, and one bounded async read."""

    name: str

    def availability(self) -> tuple[bool, str]:
        """(available, reason). ``reason`` is "" when available
        and the plain-words explanation when not."""
        ...

    async def screen_state(self) -> ScreenState:
        """One screen-state read; an unavailable or failed read
        is a ScreenState with ``available`` False, never an
        exception."""
        ...


# ---------------------------------------------------------------------------
# Conversion + distillation (pure — the tests pin these).
# ---------------------------------------------------------------------------
def _window_from_raw(raw: dict[str, Any] | None) -> WindowInfo | None:
    if not isinstance(raw, dict):
        return None
    handle = raw.get("handle")
    return WindowInfo(
        title=str(raw.get("title") or ""),
        app=str(raw.get("app") or ""),
        handle=handle if isinstance(handle, int) else None,
    )


def _element_from_raw(raw: Any, depth: int, budget: list[int]) -> ElementNode | None:
    """Convert one raw element dict, spending from the shared
    element budget. Returns None when the budget is spent or
    the raw node is unusable. Pruning rule: a node with no
    name, no value, no focus and no kept children carries no
    information for the reader and is dropped."""
    if not isinstance(raw, dict) or budget[0] <= 0:
        return None
    budget[0] -= 1
    bounds_raw = raw.get("bounds")
    bounds = None
    if (isinstance(bounds_raw, (list, tuple)) and len(bounds_raw) == 4
            and all(isinstance(v, (int, float)) for v in bounds_raw)):
        bounds = tuple(int(v) for v in bounds_raw)
    node = ElementNode(
        name=str(raw.get("name") or ""),
        role=str(raw.get("role") or ""),
        value=(str(raw["value"]) if raw.get("value") is not None else None),
        automation_id=(str(raw["automation_id"])
                       if raw.get("automation_id") is not None else None),
        focused=bool(raw.get("focused")),
        bounds=bounds,
    )
    if depth < MAX_TREE_DEPTH:
        for child_raw in raw.get("children") or []:
            child = _element_from_raw(child_raw, depth + 1, budget)
            if child is not None:
                node.children.append(child)
    if not node.name and not node.value and not node.focused \
            and not node.children:
        return None
    return node


def _convert_tree(raw: Any) -> tuple[ElementNode | None, bool]:
    """Convert the raw tree under the depth/element caps.
    Returns (root, truncated) — truncated is True when the
    element budget ran out mid-walk, i.e. the raw tree had
    more in it than the summary can show."""
    if not isinstance(raw, dict):
        return None, False
    budget = [MAX_TREE_ELEMENTS]
    root = _element_from_raw(raw, 0, budget)
    return root, budget[0] <= 0


def _clip_field(text: str) -> str:
    """One summary field: whitespace-collapsed (accessibility
    names carry newlines and padding) and length-capped."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > MAX_FIELD_CHARS:
        return collapsed[:MAX_FIELD_CHARS - 1] + "…"
    return collapsed


def _element_lines(node: ElementNode, depth: int, lines: list[str]) -> None:
    label = _clip_field(node.name)
    role = _clip_field(node.role) or "Element"
    line = "  " * depth + f"- {role}"
    if label:
        line += f' "{label}"'
    if node.value:
        line += f' value="{_clip_field(node.value)}"'
    if node.focused:
        line += " [focused]"
    lines.append(line)
    for child in node.children:
        _element_lines(child, depth + 1, lines)


def summarise_screen_state(state: ScreenState) -> str:
    """Render a ScreenState as the bounded text the supervisor
    reads. Assumes ``state.available`` — the unavailable case is
    the caller's to phrase (it has no data to summarise)."""
    lines: list[str] = []
    fg = state.foreground
    if fg is not None:
        title = _clip_field(fg.title) or "(untitled)"
        app = _clip_field(fg.app)
        lines.append(f'Foreground window: "{title}"' + (f" ({app})" if app else ""))
    else:
        lines.append("Foreground window: none reported.")
    others = state.windows
    if others:
        shown = others[:MAX_WINDOWS_LISTED]
        rendered = []
        for w in shown:
            title = _clip_field(w.title) or "(untitled)"
            app = _clip_field(w.app)
            rendered.append(f'"{title}"' + (f" ({app})" if app else ""))
        suffix = (f" — and {len(others) - len(shown)} more"
                  if len(others) > len(shown) else "")
        lines.append(f"Other visible windows ({len(others)}): "
                     + ", ".join(rendered) + suffix)
    if state.root is not None:
        lines.append("Foreground window contents "
                     "(accessibility tree, text only):")
        _element_lines(state.root, 0, lines)
        if state.tree_truncated:
            lines.append(
                f"… (element tree truncated at {MAX_TREE_ELEMENTS} "
                "elements / depth "
                f"{MAX_TREE_DEPTH} — the window holds more than "
                "the summary shows)")
    elif fg is not None:
        lines.append("The foreground window exposed no readable "
                     "accessibility elements.")
    text = "\n".join(lines)
    if len(text) > SCREEN_STATE_MAX_CHARS:
        text = text[:SCREEN_STATE_MAX_CHARS] + "\n… (screen summary truncated to budget)"
    return text


# ---------------------------------------------------------------------------
# The providers.
# ---------------------------------------------------------------------------
class UnavailableDesktopProvider:
    """The provider for every machine/situation where observe
    cannot run: a non-Windows platform, or the UIA backend's
    dependency missing. Its read is the honest refusal."""

    name = "unavailable"

    def __init__(self, reason: str):
        self._reason = reason

    def availability(self) -> tuple[bool, str]:
        return False, self._reason

    async def screen_state(self) -> ScreenState:
        return ScreenState.unavailable(self._reason)


class WindowsUiaProvider:
    """Desktop observe on Windows, over a RawDesktopAdapter
    (``windows_uia.UiautomationAdapter`` in production).

    An adapter may be injected (the tests do); with one
    injected, availability is the adapter's existence — the
    platform/package probe gates only the real backend, whose
    adapter is built lazily on first use so importing or
    probing never imports the UIA package.
    """

    name = "windows-uia"

    def __init__(self, adapter: RawDesktopAdapter | None = None, *,
                 timeout_s: float = READ_TIMEOUT_SECONDS):
        self._adapter = adapter
        self._timeout_s = timeout_s

    def availability(self) -> tuple[bool, str]:
        if self._adapter is not None:
            return True, ""
        system = platform.system()
        if system != "Windows":
            return False, (
                "Desktop observe is built on Windows UI Automation; "
                f"{system or 'this platform'} is not a supported "
                "platform for it.")
        from aether_engine.desktop.windows_uia import uia_package_state

        state = uia_package_state()
        if state is False:
            return False, (
                "The 'uiautomation' package is not installed in the "
                "engine's environment, so the accessibility tree "
                "cannot be read until it is.")
        if state is None:
            return False, (
                "Whether the 'uiautomation' package is installed "
                "could not be determined, so desktop observe is "
                "unjudged here.")
        return True, ""

    def _read_sync(self) -> dict[str, Any]:
        """The blocking read, run on a worker thread. Builds the
        real adapter on first use (its import of the UIA package
        happens here, never at probe time)."""
        adapter = self._adapter
        if adapter is None:
            from aether_engine.desktop.windows_uia import (
                UiautomationAdapter,
            )

            adapter = UiautomationAdapter()
            self._adapter = adapter
        foreground = adapter.foreground_window()
        windows = adapter.list_windows()
        tree = None
        if foreground is not None:
            tree = adapter.read_tree(
                foreground.get("handle"),
                max_depth=MAX_TREE_DEPTH,
                max_elements=MAX_TREE_ELEMENTS)
        return {"foreground": foreground, "windows": windows, "tree": tree}

    async def screen_state(self) -> ScreenState:
        available, reason = self.availability()
        if not available:
            return ScreenState.unavailable(reason)
        # The read runs on a worker thread, and the wait is a
        # plain asyncio.wait with a timeout — NOT wait_for over
        # to_thread: cancelling a task blocked on an executor
        # future does not interrupt the wait (the task stays
        # pending until the thread finishes), so a hung read
        # would hang anyway. asyncio.wait returns on timeout
        # without touching the read; the orphaned thread
        # finishes (or stays stuck) in the background and its
        # result is discarded — the engine never waits on it.
        read = asyncio.ensure_future(asyncio.to_thread(self._read_sync))
        done, _pending = await asyncio.wait(
            {read}, timeout=self._timeout_s)
        if not done:
            return ScreenState.unavailable(
                f"The desktop read did not answer within "
                f"{self._timeout_s:g} seconds — the accessibility "
                "tree may be busy; nothing was read.")
        try:
            raw = read.result()
        except Exception as exc:  # noqa: BLE001 - a failed read degrades to the honest refusal, never an engine error
            return ScreenState.unavailable(
                f"The desktop read failed: {exc}")
        foreground = _window_from_raw(raw.get("foreground"))
        # The adapter's window list includes the foreground
        # window; the summary lists it separately, so it is
        # excluded here — by handle where the backend gave one,
        # else by the first title+app match.
        raw_windows = list(raw.get("windows") or [])
        if foreground is not None:
            for i, raw_w in enumerate(raw_windows):
                if not isinstance(raw_w, dict):
                    continue
                if foreground.handle is not None:
                    match = raw_w.get("handle") == foreground.handle
                else:
                    match = (str(raw_w.get("title") or "") == foreground.title
                             and str(raw_w.get("app") or "") == foreground.app)
                if match:
                    del raw_windows[i]
                    break
        windows = [w for w in (_window_from_raw(r) for r in raw_windows)
                   if w is not None]
        root, truncated = _convert_tree(raw.get("tree"))
        return ScreenState(
            available=True,
            foreground=foreground,
            windows=windows,
            root=root,
            tree_truncated=truncated,
        )
