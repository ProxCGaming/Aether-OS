"""Screenshot capture for vision grounding (S20) — the seam.

The brief's design said "on-demand screenshot through the
existing snip plumbing". Recon found a gap worth stating
plainly: there IS no engine→renderer screenshot channel. Every
WebSocket request/response pair is renderer-initiated, and the
snip plumbing is Electron-owned and user-gesture-driven (hotkey
→ overlay → the user drags a region → the image lands in chat as
an attachment). The engine cannot pull a screenshot through it,
and inventing a covert capture channel would break the privacy
shape S19 established: the renderer must never become a screen
feed the engine can tap at will.

So grounding capture is an ENGINE-side seam instead:

- :class:`CaptureProvider` — one synchronous method returning
  PNG bytes (sync like the other raw adapters; the caller runs
  it on a worker thread under a timeout).
- :class:`WindowsGdiCapture` — the real backend, behind the
  same availability honesty as S19: a GDI screen grab
  (BitBlt + GetDIBits through ctypes — no new dependency) and
  a minimal PNG encoder over the standard library.
- Every other platform gets the honest unavailable provider.

The privacy invariants live with the CALLER (``desktop/vision.py``):
a capture happens only inside a grounding call the policy gate
already approved, the bytes are handed straight to the vision
model, and they are never written to disk, never logged, never
attached anywhere.
"""
from __future__ import annotations

import platform
import struct
import zlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class CaptureProvider(Protocol):
    """One full-screen capture, PNG-encoded."""

    def availability(self) -> tuple[bool, str]:
        """(available, reason) — probed without capturing."""
        ...

    def capture_screen(self) -> bytes | None:
        """PNG bytes of the primary screen, or None on failure."""
        ...


class UnavailableCaptureProvider:
    """The capture provider for every platform/backend where the
    grab cannot run: grounding reports honestly unavailable."""

    def __init__(self, reason: str):
        self._reason = reason

    def availability(self) -> tuple[bool, str]:
        return False, self._reason

    def capture_screen(self) -> bytes | None:
        return None


def _encode_png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """A minimal correct PNG (8-bit RGB, no filtering per row is
    legal PNG) over zlib/struct — the standard library only, so
    grounding needs no imaging dependency."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rgb_rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, level=6))
        + _chunk(b"IEND", b"")
    )


class WindowsGdiCapture:
    """Full-screen capture via GDI: GetDC → CreateCompatibleDC →
    BitBlt → GetDIBits (32-bit top-down) → PNG.

    REVIEW-ONLY like the rest of the S20 Windows backends: never
    exercised on Windows in this project's test environment.
    Coordinates: captures the primary monitor at its physical
    resolution where the process is DPI-aware (the actuation
    backend attempts awareness on first use); screen size for
    grounding maths is reported through :meth:`screen_size`.
    """

    def availability(self) -> tuple[bool, str]:
        if platform.system() != "Windows":
            return False, (
                "Screenshot capture for vision grounding is built "
                "on Windows GDI; this platform has no backend.")
        return True, ""

    def screen_size(self) -> tuple[int, int] | None:
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:  # noqa: BLE001 - size unknown; capture fails honestly too
            return None

    def capture_screen(self) -> bytes | None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        if width <= 0 or height <= 0:
            return None

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_ulong),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort),
                ("biCompression", ctypes.c_ulong),
                ("biSizeImage", ctypes.c_ulong),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", ctypes.c_ulong),
                ("biClrImportant", ctypes.c_ulong),
            ]

        screen_dc = mem_dc = bitmap = None
        try:
            screen_dc = user32.GetDC(None)
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not (screen_dc and mem_dc and bitmap):
                return None
            gdi32.SelectObject(mem_dc, bitmap)
            # SRCCOPY = 0x00CC0020
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height,
                                screen_dc, 0, 0, 0x00CC0020):
                return None
            header = _BitmapInfoHeader()
            header.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.biWidth = width
            header.biHeight = -height  # top-down rows
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0  # BI_RGB
            buffer = ctypes.create_string_buffer(width * height * 4)
            if not gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                   ctypes.byref(header), 0):
                return None
            # BGRA → RGB rows for the PNG encoder.
            raw = buffer.raw
            rows: list[bytes] = []
            stride = width * 4
            for y in range(height):
                line = raw[y * stride:(y + 1) * stride]
                row = bytearray(width * 3)
                row[0::3] = line[2::4]
                row[1::3] = line[1::4]
                row[2::3] = line[0::4]
                rows.append(bytes(row))
            return _encode_png(width, height, rows)
        except Exception:  # noqa: BLE001 - a failed capture is None, never an engine error
            return None
        finally:
            try:
                if bitmap:
                    gdi32.DeleteObject(bitmap)
                if mem_dc:
                    gdi32.DeleteDC(mem_dc)
                if screen_dc:
                    user32.ReleaseDC(None, screen_dc)
            except Exception:  # noqa: BLE001, S110 - cleanup is best-effort
                pass


_capture_override: CaptureProvider | None = None


def set_capture_provider(provider: CaptureProvider | None) -> None:
    """Replace the platform capture provider (tests)."""
    global _capture_override
    _capture_override = provider


def get_capture_provider() -> CaptureProvider:
    """The platform's capture provider (honest elsewhere)."""
    if _capture_override is not None:
        return _capture_override
    if platform.system() == "Windows":
        return WindowsGdiCapture()
    return UnavailableCaptureProvider(
        "Screenshot capture for vision grounding has no backend "
        "on this platform.")
