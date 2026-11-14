"""The Windows backend for desktop observe (master build S19).

Two halves, deliberately separated:

- :func:`uia_package_state` — the availability probe. It uses
  ``find_spec`` to locate the third-party ``uiautomation``
  package WITHOUT importing it (probing must not do the work,
  the S9/S10 pattern), and it is the only part of this module
  that runs on any platform.
- :class:`UiautomationAdapter` — the raw adapter the provider
  consumes (see ``provider.RawDesktopAdapter``). It imports
  ``uiautomation`` lazily, inside the first read, and every
  read runs on the provider's worker thread: UI Automation is
  synchronous COM work and must never touch the event loop.

This module has never run on Windows in this project's test
environment (the build VM is Linux and the package is not
installed there): the provider contract around it is fully
tested against a fake adapter, and this adapter is a thin,
defensive translation of the ``uiautomation`` package's API —
every property read is individually guarded, because a single
element whose property fetch fails (a dying app, a secure
desktop transition) must degrade that element, never the read.

READ-ONLY by construction: the adapter calls getters only —
name, control type, value pattern *read*, focus, bounds,
children, and (S20) pattern AVAILABILITY reads plus the
IsPassword property: whether an element supports Invoke or
SetValue is evidence the actuation classifier reads, and
*checking* for a pattern is a read, not an actuation. There
is still no Invoke/SetValue/SetFocus CALL anywhere in this
file; actuation lives in ``windows_actuation.py``, behind
its own module and gates.
"""
from __future__ import annotations

import ctypes
import importlib.util
import platform
from typing import Any

# The import whose presence gates the Windows backend. Mirrored
# by machine_capability.py's desktop probe (its _DESKTOP_UIA_MODULE).
UIA_PACKAGE_MODULE = "uiautomation"


def uia_package_state() -> bool | None:
    """Tri-state presence of the UIA package: True (found),
    False (not found — a determination), None (the check itself
    failed, so presence is honestly unknown). Same semantics as
    machine_capability._package_state."""
    try:
        return importlib.util.find_spec(UIA_PACKAGE_MODULE) is not None
    except (ImportError, ValueError):
        return False
    except Exception:  # noqa: BLE001 - a broken import system is "unknown"
        return None


def _process_name(process_id: int | None) -> str:
    """The executable file name for a process id, via the
    Win32 API directly (no psutil dependency). "" when the
    process cannot be named (exited, protected)."""
    if not process_id:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(process_id))
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - naming an app is best-effort
        return ""


class UiautomationAdapter:
    """RawDesktopAdapter over the ``uiautomation`` package.

    The package is imported on first read and cached; the
    adapter is constructed without importing anything, so
    building it is always safe.
    """

    def __init__(self) -> None:
        self._auto: Any = None

    def _lib(self) -> Any:
        if self._auto is None:
            import uiautomation as auto

            self._auto = auto
        return self._auto

    # -- window reads ------------------------------------------------

    def _window_dict(self, control: Any) -> dict[str, Any] | None:
        try:
            return {
                "title": str(control.Name or ""),
                "app": _process_name(
                    getattr(control, "ProcessId", None)),
                "handle": int(control.NativeWindowHandle) or None,
            }
        except Exception:  # noqa: BLE001 - one bad window must not fail the list
            return None

    def foreground_window(self) -> dict[str, Any] | None:
        auto = self._lib()
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - no foreground handle, no foreground window
            return None
        if not hwnd:
            return None
        root = auto.GetRootControl()
        for child in root.GetChildren():
            try:
                if int(child.NativeWindowHandle) == int(hwnd):
                    return self._window_dict(child)
            except Exception:  # noqa: BLE001, S112 - skip windows that cannot answer
                continue
        return None

    def list_windows(self) -> list[dict[str, Any]]:
        auto = self._lib()
        root = auto.GetRootControl()
        windows: list[dict[str, Any]] = []
        for child in root.GetChildren():
            try:
                if child.ControlTypeName != "WindowControl":
                    continue
                if child.IsOffscreen:
                    continue
            except Exception:  # noqa: BLE001, S112 - skip windows that cannot answer
                continue
            entry = self._window_dict(child)
            if entry is not None:
                windows.append(entry)
        return windows

    # -- element tree --------------------------------------------------

    def _element_value(self, control: Any) -> str | None:
        """The element's value where the OS exposes one: the
        Value pattern (edit fields, sliders), else the text of
        a text element. A read, never a set."""
        try:
            pattern = control.GetValuePattern()
            if pattern is not None:
                value = pattern.Value
                return str(value) if value is not None else None
        except Exception:  # noqa: BLE001, S110 - most elements have no value pattern
            pass
        return None

    def _pattern_present(self, control: Any, getter: str) -> bool:
        """Whether a UIA pattern is available on the element — a
        read of capability (S20's classifier evidence), guarded
        like every other property fetch here."""
        try:
            return getattr(control, getter)() is not None
        except Exception:  # noqa: BLE001 - capability unknown reads as absent
            return False

    def _element_dict(self, control: Any, depth: int, max_depth: int,
                      budget: list[int]) -> dict[str, Any] | None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            role = str(control.ControlTypeName or "").removesuffix("Control")
            bounds = None
            try:
                rect = control.BoundingRectangle
                bounds = (int(rect.left), int(rect.top),
                          int(rect.right), int(rect.bottom))
            except Exception:  # noqa: BLE001 - bounds are best-effort
                bounds = None
            try:
                is_password = bool(control.IsPassword)
            except Exception:  # noqa: BLE001 - unknown reads as not-a-password; the classifier still confirms type-ins by default posture
                is_password = False
            node: dict[str, Any] = {
                "name": str(control.Name or ""),
                "role": role,
                "value": self._element_value(control),
                "automation_id": str(control.AutomationId or "") or None,
                "focused": bool(control.HasKeyboardFocus),
                "bounds": bounds,
                "can_invoke": self._pattern_present(
                    control, "GetInvokePattern"),
                "can_set_value": self._pattern_present(
                    control, "GetValuePattern"),
                "is_password": is_password,
                "children": [],
            }
        except Exception:  # noqa: BLE001 - an unreadable element degrades to absent
            return None
        if depth < max_depth:
            try:
                children = control.GetChildren()
            except Exception:  # noqa: BLE001 - a container that cannot list children reads as a leaf
                children = []
            for child in children:
                if budget[0] <= 0:
                    break
                try:
                    if child.IsOffscreen:
                        continue
                except Exception:  # noqa: BLE001, S110 - visibility unknown: keep the element
                    pass
                entry = self._element_dict(
                    child, depth + 1, max_depth, budget)
                if entry is not None:
                    node["children"].append(entry)
        return node

    def read_tree(self, handle: int | None, *, max_depth: int,
                  max_elements: int) -> dict[str, Any] | None:
        if platform.system() != "Windows":
            return None
        auto = self._lib()
        root = auto.GetRootControl()
        target = None
        if handle:
            for child in root.GetChildren():
                try:
                    if int(child.NativeWindowHandle) == int(handle):
                        target = child
                        break
                except Exception:  # noqa: BLE001, S112 - skip windows that cannot answer
                    continue
        if target is None:
            return None
        budget = [max_elements]
        return self._element_dict(target, 0, max_depth, budget)
