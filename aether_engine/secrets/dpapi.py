"""
Encryption/decryption helpers for Aether secrets.

On Windows, ``protect``/``unprotect`` call CryptProtectData /
CryptUnprotectData via ctypes (DPAPI, bound to the current user/machine).

On non-Windows platforms, ``protect`` stores the secret in the OS keyring
(via the ``keyring`` package, service ``"aether-os"``) and returns a
``keyring:<id>`` reference; ``unprotect`` resolves that reference from
the keyring.

Any stored value that is neither a genuine DPAPI blob (Windows only) nor
a ``keyring:`` reference fails closed with :class:`SecretDecryptionError`.
There is no unauthenticated fallback decoding: a bare base64 / plaintext
blob is never silently decoded to a secret (S2-M10).
"""
from __future__ import annotations

import abc
import logging
import sys

logger = logging.getLogger(__name__)


class SecretDecryptionError(Exception):
    """Raised when a stored secret cannot be decrypted."""

class BaseProtector(abc.ABC):
    @abc.abstractmethod
    def protect(self, data: bytes) -> bytes:
        """Encrypt bytes."""

    @abc.abstractmethod
    def unprotect(self, data: bytes) -> bytes:
        """Decrypt bytes."""

    def delete(self, data: bytes) -> None:
        """Remove any external store entry backing ``data``.

        Called when the row holding a protected blob is deleted, so
        the secret does not linger in the OS keyring after its last
        reference is gone (S2-N3). Best-effort: implementations must
        not raise — a cleanup failure never fails the delete itself.
        The default has nothing external to clean (self-contained
        blobs, e.g. DPAPI) and does nothing.
        """

class WindowsDPAPIProtector(BaseProtector):
    def delete(self, data: bytes) -> None:
        # S2-N3: on Windows the DPAPI blob is self-contained — nothing
        # external to clean. Elsewhere protect() stored the secret in
        # the OS keyring under a `keyring:<id>` reference; remove that
        # entry so a deleted provider/vault row leaves no orphaned
        # secret behind. Best-effort throughout (see BaseProtector).
        if sys.platform == "win32":
            return
        try:
            secret_ref = data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return
        if not secret_ref.startswith("keyring:"):
            return
        secret_id = secret_ref.split("keyring:", 1)[1]
        if not secret_id:
            return
        try:
            import keyring

            keyring.delete_password("aether-os", secret_id)
        except Exception as exc:  # noqa: BLE001 — cleanup must never fail the delete (S2-N3)
            logger.debug("keyring cleanup failed for a deleted secret: %s", exc)

    def protect(self, data: bytes) -> bytes:
        if sys.platform == "win32":
            return _dpapi_encrypt(data)
        # Non-Windows: store in the OS keyring, return a reference to it.
        import uuid

        import keyring
        secret_id = str(uuid.uuid4())
        keyring.set_password("aether-os", secret_id, data.decode("utf-8"))
        return f"keyring:{secret_id}".encode()

    def unprotect(self, data: bytes) -> bytes:
        if sys.platform == "win32":
            return _dpapi_decrypt(data)
        # Non-Windows: the only valid stored format is a keyring reference
        # produced by protect() above. Anything else fails closed — never
        # silently decode an unauthenticated blob (e.g. bare base64) into
        # a secret (S2-M10).
        import keyring
        try:
            secret_ref = data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError) as e:
            raise SecretDecryptionError(
                "Unsupported secret format: expected a 'keyring:' reference "
                "on this platform; refusing to decode unprotected data."
            ) from e
        if not secret_ref.startswith("keyring:"):
            raise SecretDecryptionError(
                "Unsupported secret format: expected a 'keyring:' reference "
                "on this platform; refusing to decode unprotected data."
            )
        secret_id = secret_ref.split("keyring:", 1)[1]
        if not secret_id:
            raise SecretDecryptionError(
                "Unsupported secret format: expected a 'keyring:' reference "
                "on this platform; refusing to decode unprotected data."
            )
        secret = keyring.get_password("aether-os", secret_id)
        if secret is None:
            raise SecretDecryptionError("Secret not found in OS keychain.")
        return secret.encode("utf-8")


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
