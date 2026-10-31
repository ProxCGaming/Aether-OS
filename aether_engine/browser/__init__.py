"""Agent browser (Epic 8, E8-6.1).

Playwright driving a DEDICATED browser profile in a visible window.
The user's real browser and its profile are never touched: sessions
may only ever use ``~/.aether/browser-profile`` (see
:mod:`aether_engine.browser.session`). CAPTCHA / 2FA pages park the
session in a "waiting for you" state — the agent never solves
CAPTCHAs and never handles 2FA codes.

The confirmation policy surface (E8-6.2) and the credential vault
(E8-6.3) are separate slices; this package is the core they plug
into.
"""
from aether_engine.browser.session import (
    BrowserControlState,
    BrowserError,
    BrowserParkedError,
    BrowserProfileError,
    BrowserSession,
    BrowserSessionManager,
    BrowserUnavailableError,
    dedicated_profile_dir,
    get_browser_manager,
    reset_browser_manager,
    validate_profile_dir,
)

__all__ = [
    "BrowserControlState",
    "BrowserError",
    "BrowserParkedError",
    "BrowserProfileError",
    "BrowserSession",
    "BrowserSessionManager",
    "BrowserUnavailableError",
    "dedicated_profile_dir",
    "get_browser_manager",
    "reset_browser_manager",
    "validate_profile_dir",
]
