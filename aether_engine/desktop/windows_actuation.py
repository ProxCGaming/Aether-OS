"""The Windows backend for desktop actuation (master build S20).

The write-side counterpart of ``windows_uia.py``: a thin,
defensive translation of the OS APIs behind the provider's
``RawActuationAdapter`` protocol (see ``provider.py``). Kept in
its own module so the observe adapter stays read-only BY
CONSTRUCTION — this is the only file in the package that calls
Invoke/SetValue, moves the pointer, or synthesises keystrokes.

REVIEW-ONLY, stated plainly (the standing S19 caveat, extended):
this module has never run on Windows in this project's test
environment — the build VM is Linux, the ``uiautomation`` package
is not installed there, and no dependency was added for S20. The
provider/policy/tool contract around it is fully tested against
a fake adapter; this backend is written defensively (every OS
call guarded, every failure returned as a result dict, never
raised) and must be exercised on the owner's PC before any
actuation claim is made about a real machine.

Element addressing: actions arrive with a window handle and a
MATCHER (automation_id / name / role from the resolved element).
UIA element references do not survive across reads, so the
backend re-finds the live control under the window at action
time, with the same matching rule the provider resolves with
(automation_id exact wins; else name+role).
"""
from __future__ import annotations

import ctypes
import logging
import platform
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Cap on the element search walk — a pathological tree must not
#: turn one action into an unbounded search (the actuator's own
#: timeout is the outer bound; this is the inner one).
MAX_SEARCH_ELEMENTS = 4000

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _ok(detail: str = "") -> dict[str, Any]:
    return {"ok": True, "detail": detail}


def _fail(detail: str) -> dict[str, Any]:
    return {"ok": False, "detail": detail}


class UiautomationActuationAdapter:
    """RawActuationAdapter over ``uiautomation`` + Win32.

    Constructed without importing anything (the provider builds
    it lazily on first action); the package import happens on
    first use, on the actuator's worker thread.
    """

    def __init__(self) -> None:
        self._auto: Any = None
        self._dpi_attempted = False

    def _lib(self) -> Any:
        if self._auto is None:
            import uiautomation as auto

            self._auto = auto
        self._ensure_dpi_aware()
        return self._auto

    def _ensure_dpi_aware(self) -> None:
        """Best-effort per-monitor DPI awareness, once: UIA bounds
        and SendInput coordinates must live in the same (physical
        pixel) space or clicks land offset on scaled displays."""
        if self._dpi_attempted:
            return
        self._dpi_attempted = True
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2 via the legacy call
            # (available since Windows 8.1, no context API needed).
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        except Exception:  # awareness is best-effort; UIA and input both fall back to the same virtualised space
            logger.debug("DPI awareness call failed", exc_info=True)

    # -- element finding --------------------------------------------

    def _find_window(self, handle: int | None) -> Any | None:
        auto = self._lib()
        if not handle:
            return None
        root = auto.GetRootControl()
        for child in root.GetChildren():
            try:
                if int(child.NativeWindowHandle) == int(handle):
                    return child
            except Exception:  # noqa: BLE001, S112 - skip windows that cannot answer
                continue
        return None

    def _find_element(self, window: Any,
                      matcher: dict[str, Any]) -> Any | None:
        automation_id = str(matcher.get("automation_id") or "")
        name = str(matcher.get("name") or "").strip().lower()
        role = str(matcher.get("role") or "").strip().lower()
        visited = [0]
        by_id: list[Any] = []
        by_name: list[Any] = []

        def _walk(control: Any) -> None:
            if visited[0] >= MAX_SEARCH_ELEMENTS:
                return
            visited[0] += 1
            try:
                if automation_id and str(control.AutomationId or "") == automation_id:
                    by_id.append(control)
                elif name and str(control.Name or "").strip().lower() == name:
                    control_role = str(control.ControlTypeName or "") \
                        .removesuffix("Control").strip().lower()
                    if not role or control_role == role:
                        by_name.append(control)
            except Exception:  # noqa: BLE001, S110 - an element that cannot answer is not a match
                pass
            try:
                children = control.GetChildren()
            except Exception:  # noqa: BLE001 - a container that cannot list children is a leaf here
                children = []
            for child in children:
                _walk(child)

        _walk(window)
        if by_id:
            return by_id[0]
        if len(by_name) == 1:
            return by_name[0]
        return None

    def _resolve(self, handle: int | None,
                 matcher: dict[str, Any]) -> tuple[Any | None, str]:
        if platform.system() != "Windows":
            return None, "Desktop actuation requires Windows."
        window = self._find_window(handle)
        if window is None:
            return None, ("The target window is no longer open "
                          "(it may have closed since the read).")
        element = self._find_element(window, matcher)
        if element is None:
            return None, ("The target element could not be found "
                          "again — the screen may have changed "
                          "since the read.")
        return element, ""

    # -- element actions ----------------------------------------------

    def invoke_element(self, handle: int | None,
                       matcher: dict[str, Any]) -> dict[str, Any]:
        try:
            element, error = self._resolve(handle, matcher)
            if element is None:
                return _fail(error)
            pattern = element.GetInvokePattern()
            if pattern is None:
                return _fail(
                    "The element does not support Invoke (it may "
                    "need a real click instead).")
            pattern.Invoke()
            return _ok("Invoked.")
        except Exception as exc:  # noqa: BLE001 - actuation failures are results, never exceptions
            return _fail(f"Invoke failed: {exc}")

    def set_element_value(self, handle: int | None,
                          matcher: dict[str, Any],
                          value: str) -> dict[str, Any]:
        try:
            element, error = self._resolve(handle, matcher)
            if element is None:
                return _fail(error)
            pattern = element.GetValuePattern()
            if pattern is None:
                return _fail(
                    "The element does not accept a settable value "
                    "(it may be read-only, or need real keystrokes).")
            if getattr(pattern, "IsReadOnly", False):
                return _fail("The element is read-only.")
            pattern.SetValue(value)
            return _ok("Value set.")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Setting the value failed: {exc}")

    # -- window primitives ----------------------------------------------

    def focus_window(self, handle: int | None) -> dict[str, Any]:
        try:
            if not handle:
                return _fail("No window handle to focus.")
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # Restore first when minimised (SW_RESTORE = 9), then
            # bring to the foreground.
            user32.ShowWindow(ctypes.c_void_p(int(handle)), 9)
            if user32.SetForegroundWindow(ctypes.c_void_p(int(handle))):
                return _ok("Window brought to the front.")
            return _fail(
                "Windows refused to move the window to the "
                "foreground (foreground rules may block it).")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Focusing the window failed: {exc}")

    def close_window(self, handle: int | None) -> dict[str, Any]:
        try:
            if platform.system() != "Windows":
                return _fail("Desktop actuation requires Windows.")
            window = self._find_window(handle)
            if window is None:
                return _fail("The target window is no longer open.")
            pattern = window.GetWindowPattern()
            if pattern is not None:
                pattern.Close()
                return _ok("Window closed.")
            # Fallback: the polite WM_CLOSE (0x0010) — the app
            # still decides what closing means (save prompts etc.).
            ctypes.windll.user32.PostMessageW(  # type: ignore[attr-defined]
                ctypes.c_void_p(int(handle)), 0x0010, 0, 0)
            return _ok("Close requested.")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Closing the window failed: {exc}")

    # -- clipboard -------------------------------------------------------

    def clipboard_get(self) -> dict[str, Any]:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if not _open_clipboard(user32):
                return _fail("The clipboard is busy (another app holds it open).")
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return {"ok": True, "text": "",
                            "detail": "The clipboard holds no text."}
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return _fail("The clipboard data could not be locked.")
                try:
                    text = ctypes.c_wchar_p(pointer).value or ""
                finally:
                    kernel32.GlobalUnlock(handle)
                return {"ok": True, "text": text, "detail": ""}
            finally:
                user32.CloseClipboard()
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Reading the clipboard failed: {exc}")

    def clipboard_set(self, text: str) -> dict[str, Any]:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if not _open_clipboard(user32):
                return _fail("The clipboard is busy (another app holds it open).")
            try:
                user32.EmptyClipboard()
                data = str(text).encode("utf-16-le") + b"\x00\x00"
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not handle:
                    return _fail("Clipboard memory could not be allocated.")
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    kernel32.GlobalFree(handle)
                    return _fail("Clipboard memory could not be locked.")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
                    return _fail("Setting the clipboard data failed.")
                # Ownership of the handle passed to the system.
                return _ok("Clipboard replaced.")
            finally:
                user32.CloseClipboard()
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Setting the clipboard failed: {exc}")

    # -- pixel input ------------------------------------------------------

    def mouse_click(self, x: int, y: int) -> dict[str, Any]:
        try:
            self._lib()
            inputs = [
                _mouse_input(x, y, 0x8001),         # MOVE | ABSOLUTE
                _mouse_input(x, y, 0x0002),         # LEFTDOWN
                _mouse_input(x, y, 0x0004),         # LEFTUP
            ]
            if not _send_inputs(inputs):
                return _fail("Windows refused the synthetic click.")
            return _ok(f"Clicked at ({x}, {y}).")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"The click failed: {exc}")

    def type_text(self, text: str) -> dict[str, Any]:
        try:
            self._lib()
            inputs = []
            # UTF-16 code units: KEYEVENTF_UNICODE takes one unit
            # per event, surrogate pairs included.
            units = str(text).encode("utf-16-le")
            for offset in range(0, len(units), 2):
                unit = int.from_bytes(units[offset:offset + 2], "little")
                inputs.append(_key_input(unit, 0x0004))          # UNICODE down
                inputs.append(_key_input(unit, 0x0004 | 0x0002))  # + KEYUP
            if inputs and not _send_inputs(inputs):
                return _fail("Windows refused the synthetic keystrokes.")
            return _ok(f"Typed {len(str(text))} characters.")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Typing failed: {exc}")

    def key_press(self, keys: str) -> dict[str, Any]:
        try:
            self._lib()
            parts = [p.strip().lower() for p in str(keys).split("+") if p.strip()]
            if not parts:
                return _fail("No keys given.")
            if {"ctrl", "alt"} <= set(parts) and set(parts) & {"del", "delete"}:
                return _fail(
                    "Ctrl+Alt+Del is the secure attention sequence: "
                    "Windows does not let any program synthesise it.")
            virtual_keys: list[int] = []
            for part in parts:
                vk = _virtual_key(part)
                if vk is None:
                    return _fail(f"Unknown key: {part!r}.")
                virtual_keys.append(vk)
            *modifiers, main_key = virtual_keys
            inputs = [_key_input(vk, 0) for vk in modifiers]
            inputs.append(_key_input(main_key, 0))
            inputs.append(_key_input(main_key, 0x0002))
            inputs += [_key_input(vk, 0x0002) for vk in reversed(modifiers)]
            if not _send_inputs(inputs):
                return _fail("Windows refused the synthetic key press.")
            return _ok(f"Pressed {keys}.")
        except Exception as exc:  # noqa: BLE001
            return _fail(f"The key press failed: {exc}")


def _open_clipboard(user32: Any, attempts: int = 10) -> bool:
    """OpenClipboard with a short retry: the clipboard is a
    single-owner resource and transient contention is normal."""
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------
# SendInput plumbing (ctypes structures).
# ---------------------------------------------------------------------

class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KeybdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeybdInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002


def _mouse_input(x: int, y: int, flags: int) -> _Input:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    width = max(1, int(user32.GetSystemMetrics(0)))    # SM_CXSCREEN
    height = max(1, int(user32.GetSystemMetrics(1)))   # SM_CYSCREEN
    absolute_x = int(x * 65535 / max(1, width - 1))
    absolute_y = int(y * 65535 / max(1, height - 1))
    entry = _Input()
    entry.type = INPUT_MOUSE
    entry.mi = _MouseInput(
        dx=absolute_x, dy=absolute_y, mouseData=0,
        dwFlags=flags | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=None)
    return entry


def _key_input(code: int, flags: int) -> _Input:
    entry = _Input()
    entry.type = INPUT_KEYBOARD
    if flags & 0x0004:  # KEYEVENTF_UNICODE: the code is a UTF-16 unit in wScan
        entry.ki = _KeybdInput(wVk=0, wScan=code, dwFlags=flags,
                               time=0, dwExtraInfo=None)
    else:
        entry.ki = _KeybdInput(wVk=code, wScan=0, dwFlags=flags,
                               time=0, dwExtraInfo=None)
    return entry


def _send_inputs(inputs: list[_Input]) -> bool:
    if not inputs:
        return True
    array = (_Input * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(  # type: ignore[attr-defined]
        len(inputs), array, ctypes.sizeof(_Input))
    return int(sent) == len(inputs)


_NAMED_KEYS: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08, "bksp": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},
}


def _virtual_key(part: str) -> int | None:
    if part in _NAMED_KEYS:
        return _NAMED_KEYS[part]
    if len(part) == 1:
        try:
            result = ctypes.windll.user32.VkKeyScanW(ord(part))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - unresolvable character
            return None
        vk = int(result) & 0xFF
        return vk or None
    return None
