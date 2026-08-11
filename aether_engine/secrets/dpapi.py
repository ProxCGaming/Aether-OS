"""
Windows DPAPI encryption/decryption helpers for Aether secrets.

Uses ctypes to call CryptProtectData / CryptUnprotectData via the Windows API.
Falls back to a simple XOR obfuscation on non-Windows platforms (dev/test only).
"""
from __future__ import annotations

import base64
import os
import sys


import abc

class SecretDecryptionError(Exception):
    """Raised when a stored secret cannot be decrypted."""

class BaseProtector(abc.ABC):
    @abc.abstractmethod
    def protect(self, data: bytes) -> bytes:
        """Encrypt bytes."""
        pass

    @abc.abstractmethod
    def unprotect(self, data: bytes) -> bytes:
        """Decrypt bytes."""
        pass

class WindowsDPAPIProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        if sys.platform == "win32":
            return _dpapi_encrypt(data)
        # Non-Windows fallback: base64 encoding (not secure, dev only)
        return base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        if sys.platform == "win32":
            return _dpapi_decrypt(data)
        # Non-Windows fallback
        return base64.b64decode(data)


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def _dpapi_encrypt(data: bytes) -> bytes:
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        blob_in = _DATA_BLOB(ctypes.sizeof(buf), buf)
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            raise RuntimeError(f"CryptProtectData failed: {_kernel32.GetLastError()}")
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        _kernel32.LocalFree(blob_out.pbData)
        return encrypted

    def _dpapi_decrypt(data: bytes) -> bytes:
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        blob_in = _DATA_BLOB(ctypes.sizeof(buf), buf)
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            raise SecretDecryptionError(
                f"CryptUnprotectData failed (code {_kernel32.GetLastError()}). "
                "The secret may have been encrypted by a different user or machine."
            )
        plaintext = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        _kernel32.LocalFree(blob_out.pbData)
        return plaintext.rstrip(b'\x00')
