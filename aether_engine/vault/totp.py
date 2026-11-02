"""TOTP computation for the vault's authenticator view.

PRD Open Point 14 (DECIDED YES by the owner, 1 Nov 2026): the
credential vault doubles as the owner's authenticator app. This
module is the arithmetic half — RFC 6238 (TOTP) over RFC 4226
(HOTP) with HMAC-SHA1, a 30-second period and 6 digits, the
profile every mainstream authenticator enrols.

Pure functions only: no storage, no I/O, no clocks read implicitly
(every function takes the time explicitly, so tests can pin the
RFC's published vectors). Secrets reach this module from the
owner-only accessors in :mod:`aether_engine.vault.store`; no agent
path can call it — agents never retrieve TOTP codes (grill Q35).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct

PERIOD_SECONDS = 30
CODE_DIGITS = 6

_B32_BODY = re.compile(r"^[A-Z2-7]+$")


class TotpValidationError(ValueError):
    """A TOTP secret is not well-formed Base32 (RFC 4648)."""


def normalize_base32_secret(raw: str) -> str:
    """Canonical form of a Base32 TOTP secret, or raise.

    Setup keys are shown to humans in groups, sometimes hyphenated
    or lower-case, padding optional — all cosmetic. What remains
    must be non-empty, use only the RFC 4648 alphabet (A–Z, 2–7 —
    note 0, 1, 8, 9 never appear), have a length a real encoding
    can produce, and decode.
    """
    if raw is None:
        raise TotpValidationError("A TOTP secret is required.")
    text = re.sub(r"[\s-]+", "", str(raw)).upper().rstrip("=")
    if not text:
        raise TotpValidationError("A TOTP secret is required.")
    if not _B32_BODY.match(text):
        raise TotpValidationError(
            "That TOTP secret is not valid Base32: setup keys use "
            "only the letters A-Z and the digits 2-7.")
    if len(text) % 8 in (1, 3, 6):
        raise TotpValidationError(
            "That TOTP secret is not valid Base32: its length "
            "cannot come from a real setup key.")
    try:
        _decode(text)
    except (ValueError, TypeError) as exc:
        raise TotpValidationError(
            "That TOTP secret is not valid Base32.") from exc
    return text


def _decode(normalized: str) -> bytes:
    return base64.b32decode(normalized + "=" * (-len(normalized) % 8))


def totp_code(secret_b32: str, at_time: float,
              *, digits: int = CODE_DIGITS,
              period: int = PERIOD_SECONDS) -> str:
    """The code for the period containing ``at_time`` (RFC 6238 §5).

    HOTP with the period counter as the moving factor and RFC 4226
    dynamic truncation, reduced mod 10**digits and zero-padded —
    the truncation under which the RFC's 8-digit Appendix B values
    end in the familiar 6-digit codes (e.g. T=59 → 94287082 →
    "287082").
    """
    key = _decode(normalize_base32_secret(secret_b32))
    counter = int(at_time) // period
    digest = hmac.new(key, struct.pack(">Q", counter),
                      hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def seconds_remaining(at_time: float,
                      *, period: int = PERIOD_SECONDS) -> int:
    """Seconds left in the period containing ``at_time``."""
    return period - (int(at_time) % period)
