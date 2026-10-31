"""Browser confirmation policy: taxonomy, envelopes, gate (E8-6.2).

Epic 8, E8-6.2 (grill Q32–Q35). The browser core (E8-6.1) made
every browser write a generic approval. This module is the
per-category confirmation taxonomy that replaces that blanket ask:

- **Free** — browsing, reading, searching, filling ordinary form
  fields. Never gated.
- **Consequential** — payments, sends/publishes, deletes, account
  changes. Each category has an owner-controlled mode (Settings →
  Browser Agent Policy): ``always_confirm`` / ``allow_with_envelope``
  / ``allow_freely``.
- **Unknown** — anything the classifier cannot prove free or
  consequential. Unknown ALWAYS confirms (fail-closed): there is no
  path by which an unclassified action proceeds silently.

A per-task **envelope** is a bounded pre-authorisation the owner
grants for one task (one category, one site, an action-count and/or
spend bound, an expiry). Envelopes never cover payments — payments
confirm individually, whatever the mode says. The shipped preset is
Always-confirm for every category, envelopes enabled, vault autofill
off, browser agent on.

Classification is deliberately simple marker matching — the same
style as :func:`aether_engine.browser.tools.detect_challenge` —
over the pending call's arguments and the session's cached page
snapshot (URL, title, HTML). The exact-action description shown on
the confirmation card ("Pay ₹499 to Merchant") is built ONLY from
page data: an amount or merchant the page does not state is never
invented; the card falls back to plainer wording instead.

The gate itself lives in the graph's approval pause
(``langgraph/nodes/pause.py``), which consults
:func:`evaluate_browser_action`; the session layer consults
:func:`current_browser_policy` so the master switch can block a
NEW browser session from starting while never killing a live one.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("aether_engine.browser.policy")

# ---------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------

CATEGORY_PAYMENTS = "payments"
CATEGORY_SENDS = "sends_publishes"
CATEGORY_DELETES = "deletes"
CATEGORY_ACCOUNT = "account_changes"
CATEGORIES = (
    CATEGORY_PAYMENTS,
    CATEGORY_SENDS,
    CATEGORY_DELETES,
    CATEGORY_ACCOUNT,
)

CATEGORY_FREE = "free"
CATEGORY_UNKNOWN = "unknown"

MODE_ALWAYS_CONFIRM = "always_confirm"
MODE_ALLOW_WITH_ENVELOPE = "allow_with_envelope"
MODE_ALLOW_FREELY = "allow_freely"
MODES = (MODE_ALWAYS_CONFIRM, MODE_ALLOW_WITH_ENVELOPE, MODE_ALLOW_FREELY)

BROWSER_TOOL_NAMES = frozenset({
    "browser_navigate", "browser_click", "browser_type", "browser_extract",
})

DEFAULT_ENVELOPE_TTL_SECONDS = 3600.0
# Bounds applied when an envelope is granted from a confirmation
# card (the grant's category and site always come from the confirmed
# action itself — the UI only opts in, it cannot widen the scope).
DEFAULT_ENVELOPE_MAX_ACTIONS = 20

_CURRENCY_BY_SYMBOL = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "Rs": "INR"}


# ---------------------------------------------------------------------
# Policy value object + UserConfig plumbing
# ---------------------------------------------------------------------

def default_category_modes() -> dict[str, str]:
    """The shipped preset: every category confirms (Q32)."""
    return {category: MODE_ALWAYS_CONFIRM for category in CATEGORIES}


def clean_category_modes(raw: Any) -> dict[str, str]:
    """Validate a raw category→mode mapping.

    Unknown categories are dropped; unknown modes fall back to
    ``always_confirm`` (a corrupt policy file must tighten, never
    loosen, the confirmation posture).
    """
    modes = default_category_modes()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in modes and value in MODES:
                modes[key] = value
    return modes


@dataclass(frozen=True)
class BrowserPolicy:
    """The owner's browser-agent policy (Settings → Browser Agent)."""

    enabled: bool = True
    vault_autofill: bool = False
    envelopes_enabled: bool = True
    categories: dict[str, str] = field(default_factory=default_category_modes)

    def mode_for(self, category: str) -> str:
        if category not in CATEGORIES:
            return MODE_ALWAYS_CONFIRM
        return self.categories.get(category, MODE_ALWAYS_CONFIRM)

    def to_dict(self) -> dict[str, Any]:
        """The BROWSER_POLICY_GET_RESPONSE payload shape."""
        return {
            "enabled": bool(self.enabled),
            "vault_autofill": bool(self.vault_autofill),
            "envelopes_enabled": bool(self.envelopes_enabled),
            "categories": {c: self.mode_for(c) for c in CATEGORIES},
        }

    @classmethod
    def from_dict(cls, data: Any) -> BrowserPolicy:
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            vault_autofill=bool(data.get("vault_autofill", False)),
            envelopes_enabled=bool(data.get("envelopes_enabled", True)),
            categories=clean_category_modes(data.get("categories")),
        )


def policy_from_user_config(user_config: Any) -> BrowserPolicy:
    """Build the policy from a UserConfig (defensive getattr reads,
    so partially-built configs in tests still resolve to the preset)."""
    if user_config is None:
        return BrowserPolicy()
    return BrowserPolicy(
        enabled=bool(getattr(user_config, "browser_agent_enabled", True)),
        vault_autofill=bool(getattr(user_config, "browser_vault_autofill", False)),
        envelopes_enabled=bool(getattr(user_config, "browser_envelopes_enabled", True)),
        categories=clean_category_modes(
            getattr(user_config, "browser_category_modes", None)),
    )


def apply_policy_update(user_config: Any, payload: Any) -> tuple[bool, str]:
    """Validate + apply a BROWSER_POLICY_SET_REQUEST payload.

    Partial-update semantics: fields that are absent or None are left
    unchanged. Everything is validated BEFORE anything is mutated,
    so a rejected update leaves the policy exactly as it was.
    Returns ``(ok, error)``.
    """
    if not isinstance(payload, dict):
        return False, "Policy update payload must be an object."
    toggles: dict[str, bool] = {}
    for key in ("enabled", "vault_autofill", "envelopes_enabled"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            return False, f"'{key}' must be true or false."
        toggles[key] = value
    categories = payload.get("categories")
    if categories is not None:
        if not isinstance(categories, dict):
            return False, "'categories' must be an object of category → mode."
        for key, value in categories.items():
            if key not in CATEGORIES:
                return False, f"Unknown browser policy category: {key!r}."
            if value not in MODES:
                return False, f"Unknown mode for {key}: {value!r}."
    # Fully validated — mutate.
    if "enabled" in toggles:
        user_config.browser_agent_enabled = toggles["enabled"]
    if "vault_autofill" in toggles:
        user_config.browser_vault_autofill = toggles["vault_autofill"]
    if "envelopes_enabled" in toggles:
        user_config.browser_envelopes_enabled = toggles["envelopes_enabled"]
    if categories:
        merged = clean_category_modes(getattr(user_config, "browser_category_modes", None))
        merged.update(categories)
        user_config.browser_category_modes = merged
    return True, ""


# ---------------------------------------------------------------------
# Policy loader seam (the engine installs the live config reader,
# mirroring tools/web_search.set_search_config_loader)
# ---------------------------------------------------------------------

_policy_loader: Callable[[], BrowserPolicy] | None = None


def set_browser_policy_loader(
    loader: Callable[[], BrowserPolicy] | None,
) -> None:
    global _policy_loader
    _policy_loader = loader


def current_browser_policy() -> BrowserPolicy:
    """The live policy, or the shipped preset when no loader is
    installed / the loader fails (the preset is the safe fallback)."""
    if _policy_loader is None:
        return BrowserPolicy()
    try:
        policy = _policy_loader()
    except Exception:
        logger.debug("Browser policy loader failed; using the preset", exc_info=True)
        return BrowserPolicy()
    return policy if isinstance(policy, BrowserPolicy) else BrowserPolicy()


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ClassifiedAction:
    """One browser action, classified for the gate."""

    tool_name: str
    category: str  # CATEGORIES member | "free" | "unknown"
    description: str  # the exact action, for the confirmation card
    site_host: str = ""
    site_name: str = ""
    amount: float | None = None
    amount_text: str | None = None  # e.g. "₹499", exactly as the page states it
    currency: str | None = None

    def confirmation_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "site_host": self.site_host,
            "site_name": self.site_name,
            "amount": self.amount,
            "amount_text": self.amount_text,
            "currency": self.currency,
            "tool_name": self.tool_name,
        }


_PAYMENT_MARKERS = (
    "pay now", "pay ₹", "pay $", "pay €", "pay £", "place order",
    "confirm payment", "complete payment",
    "make payment", "complete purchase", "confirm order",
    "complete booking", "book now", "reserve now", "confirm booking",
    "checkout", "buy now", "pay securely",
)
_SEND_MARKERS = (
    "send message", "send email", "send now", "publish", "tweet",
    "post comment", "post now", "share post", "send", "post",
)
_DELETE_MARKERS = (
    "delete", "remove", "move to trash", "discard", "destroy", "erase",
)
_ACCOUNT_MARKERS = (
    "change password", "update password", "reset password",
    "change email", "update email", "change phone", "update phone",
    "update profile", "edit profile", "deactivate", "close account",
    "manage account", "account settings", "security settings",
)
# Verbs that commit SOMETHING but say nothing about what. When one of
# these is the whole signal, the action is unknown — never free.
_AMBIGUOUS_VERBS = (
    "submit", "confirm", "proceed", "continue", "done", "apply", "ok", "yes",
)
_CARD_FIELD_MARKERS = (
    "cc-number", "cc_number", "cc-num", "cardnumber", "card-number",
    "card number", "credit-card", "creditcard", "credit card",
    "cvv", "cvc", "cc-exp", "card expiry", "card expiration",
)
_PASSWORD_FIELD_MARKERS = (
    'type="password"', "type='password'", "current-password",
    "new-password", "password",
)
_ACCOUNT_URL_MARKERS = ("/settings", "/account", "/profile", "/preferences")
_ACCOUNT_SAVE_MARKERS = ("save", "update", "apply changes")

_AMOUNT_RE = re.compile(r"(₹|\$|€|£|Rs\.?)\s?(\d[\d,]*(?:\.\d{1,2})?)")
_TOTAL_AMOUNT_RE = re.compile(
    r"(?:total|amount|payable|due)[^₹$€£\d]{0,24}(₹|\$|€|£|Rs\.?)\s?(\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_CONTAINER_TAG_RE = re.compile(
    r"<(button|a|textarea|select)\b([^>]*)>(.*?)</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INPUT_TAG_RE = re.compile(r"<input\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)


def _visible_text(html: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", html or "",
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _site_name(title: str, host: str) -> str:
    cleaned = re.split(r"\s*[|—–·»]\s*|\s+-\s+", (title or "").strip())[0].strip()
    return cleaned or host


def _parse_amount(text: str) -> tuple[float, str, str] | None:
    """(value, amount_text, currency) from the first stated amount."""
    match = _AMOUNT_RE.search(text or "")
    if not match:
        return None
    symbol = match.group(1).rstrip(".")
    try:
        value = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    return value, match.group(0).strip(), _CURRENCY_BY_SYMBOL.get(symbol, "")


def _page_amount(html: str) -> tuple[float, str, str] | None:
    """An order-total amount stated on the page (never invented)."""
    match = _TOTAL_AMOUNT_RE.search(_visible_text(html))
    if not match:
        return None
    symbol = match.group(1).rstrip(".")
    try:
        value = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    return value, f"{match.group(1)} {match.group(2)}".strip(), _CURRENCY_BY_SYMBOL.get(symbol, "")


@dataclass
class _Element:
    tag: str
    attrs: str
    text: str  # visible inner text, whitespace-collapsed

    @property
    def evidence(self) -> str:
        return f"{self.attrs} {self.text}".lower()


def _iter_elements(html: str) -> list[_Element]:
    elements: list[_Element] = []
    for match in _CONTAINER_TAG_RE.finditer(html or ""):
        elements.append(_Element(
            tag=match.group(1).lower(),
            attrs=match.group(2),
            text=_visible_text(match.group(3)),
        ))
    for match in _INPUT_TAG_RE.finditer(html or ""):
        elements.append(_Element(tag="input", attrs=match.group(1), text=""))
    return elements


def _selector_hints(selector: str) -> list[tuple[str, str]]:
    """(kind, value) hints from a Playwright-ish selector, strongest
    first: id, attribute equality, text target, class."""
    hints: list[tuple[str, str]] = []
    sel = (selector or "").strip()
    if sel.lower().startswith("text="):
        target = sel[5:].strip().strip("\"'")
        if target:
            hints.append(("text", target))
        return hints
    id_match = re.search(r"#([\w-]+)", sel)
    if id_match:
        hints.append(("id", id_match.group(1)))
    for attr_match in re.finditer(r"\[([\w-]+)(?:\s*=\s*[\"']?([^\"'\]]+)[\"']?)?\]", sel):
        if attr_match.group(2):
            hints.append(("attr", f'{attr_match.group(1)}="{attr_match.group(2)}"'))
    for class_match in re.finditer(r"\.([\w-]+)", sel):
        hints.append(("class", class_match.group(1)))
    return hints


def _find_element(selector: str, html: str) -> _Element | None:
    """The page element a selector most likely targets, or None.

    Best-effort by design — the fail-closed gate treats "not found"
    as "cannot prove what this does", never as "harmless".
    """
    hints = _selector_hints(selector)
    if not hints:
        return None
    elements = _iter_elements(html)
    for kind, value in hints:
        for element in elements:
            attrs = element.attrs
            if kind == "id" and re.search(rf"id\s*=\s*[\"']?{re.escape(value)}[\"']?(\s|>|$)", attrs + ">"):
                return element
            if kind == "attr" and value.lower() in attrs.lower():
                return element
            if kind == "class" and re.search(rf"class\s*=\s*[\"'][^\"']*\b{re.escape(value)}\b", attrs):
                return element
            if kind == "text" and value.lower() in element.text.lower():
                return element
    return None


def _has_marker(evidence: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in evidence:
            return marker
    return None


def classify_browser_action(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    url: str = "",
    title: str = "",
    content: str = "",
) -> ClassifiedAction:
    """Classify one pending browser action for the gate.

    ``url``/``title``/``content`` are the CURRENT page snapshot (the
    page the action would act on), from the browser session's cache.
    Pure and total: any doubt resolves to ``unknown``, never free.
    """
    args = args or {}
    page_url = url or (str(args.get("url", "")) if tool_name == "browser_navigate" else "")
    host = urlparse(page_url).netloc if page_url else ""
    site = _site_name(title, host)

    def _action(category: str, description: str, **kw: Any) -> ClassifiedAction:
        return ClassifiedAction(
            tool_name=tool_name, category=category, description=description,
            site_host=host, site_name=site, **kw)

    if tool_name == "browser_extract":
        return _action(CATEGORY_FREE, f"Read the page on {site}")
    if tool_name == "browser_navigate":
        return _action(CATEGORY_FREE, f"Open {host or page_url}")
    if tool_name == "browser_click":
        return _classify_click(args, page_url=page_url, title=title,
                               content=content, site=site, make=_action)
    if tool_name == "browser_type":
        return _classify_type(args, content=content, site=site, make=_action)
    # A browser-named tool this taxonomy does not know: fail closed.
    return _action(CATEGORY_UNKNOWN,
                   f"Unrecognised browser action ({tool_name}) on {site}")


def _classify_click(args, *, page_url, title, content, site, make):
    selector = str(args.get("selector", ""))
    element = _find_element(selector, content)
    evidence_parts = [selector.lower()]
    if element is not None:
        evidence_parts.append(element.evidence)
    evidence = " ".join(evidence_parts)
    element_text = element.text if element is not None else ""

    def _payment() -> ClassifiedAction:
        parsed = _parse_amount(element_text) or _parse_amount(selector) \
            or _page_amount(content)
        if parsed is not None:
            value, amount_text, currency = parsed
            return make(CATEGORY_PAYMENTS, f"Pay {amount_text} to {site}",
                        amount=value, amount_text=amount_text, currency=currency)
        if element_text:
            return make(CATEGORY_PAYMENTS, f"{element_text} on {site}")
        return make(CATEGORY_PAYMENTS, f"Complete a payment on {site}")

    if _has_marker(evidence, _PAYMENT_MARKERS) or re.match(
            r"pay\b", element_text.strip().lower()):
        return _payment()
    if _has_marker(evidence, _DELETE_MARKERS):
        label = element_text or "Delete"
        return make(CATEGORY_DELETES, f"{label} on {site}")
    if _has_marker(evidence, _SEND_MARKERS):
        label = element_text or "Send or publish"
        return make(CATEGORY_SENDS, f"{label} on {site}")
    if _has_marker(evidence, _ACCOUNT_MARKERS):
        label = element_text or "Change account settings"
        return make(CATEGORY_ACCOUNT, f"{label} on {site}")
    # A generic save/update on an account-context page changes the
    # account; anywhere else a bare ambiguous verb proves nothing.
    lowered_text = element_text.strip().lower()
    if any(m in page_url.lower() for m in _ACCOUNT_URL_MARKERS) and \
            _has_marker(lowered_text, _ACCOUNT_SAVE_MARKERS):
        label = element_text or "Save account changes"
        return make(CATEGORY_ACCOUNT, f"{label} on {site}")
    if lowered_text in _AMBIGUOUS_VERBS or (
        element is None and selector.strip().lower() in _AMBIGUOUS_VERBS
    ):
        return make(CATEGORY_UNKNOWN,
                    f"Unrecognised browser action on {site} (browser_click)")
    if element is not None and (
        element_text
        or element.tag == "a"
        or "aria-label" in element.attrs.lower()
        or "title=" in element.attrs.lower()
    ):
        # A resolved, identifiable element with no consequential
        # signal: ordinary navigation / UI (links, menus,
        # add-to-cart, sign-in…). An element with NO identifying
        # evidence at all (no text, no label) proves nothing and
        # falls through to unknown.
        label = element_text or "Browse"
        return make(CATEGORY_FREE, f"{label} on {site}")
    return make(CATEGORY_UNKNOWN,
                f"Unrecognised browser action on {site} (browser_click)")


def _classify_type(args, *, content, site, make):
    selector = str(args.get("selector", ""))
    element = _find_element(selector, content)
    evidence_parts = [selector.lower()]
    if element is not None:
        evidence_parts.append(element.attrs.lower())
    evidence = " ".join(evidence_parts)

    if _has_marker(evidence, _CARD_FIELD_MARKERS):
        return make(CATEGORY_PAYMENTS, f"Enter payment card details on {site}")
    if _has_marker(evidence, _PASSWORD_FIELD_MARKERS):
        # Never free, and the typed text is never echoed anywhere.
        return make(CATEGORY_UNKNOWN, f"Type into a password field on {site}")
    if element is not None and element.tag in ("input", "textarea", "select"):
        return make(CATEGORY_FREE, f"Fill a form field on {site}")
    return make(CATEGORY_UNKNOWN, f"Type into an unidentified field on {site}")


def page_context_from_session(session: Any) -> dict[str, str]:
    """The classifier's page snapshot from a browser session (the
    session caches title + HTML at every navigate/click, E8-6.2)."""
    if session is None:
        return {"url": "", "title": "", "content": ""}
    return {
        "url": getattr(session, "current_url", "") or "",
        "title": getattr(session, "last_title", "") or "",
        "content": getattr(session, "last_page_content", "") or "",
    }


# ---------------------------------------------------------------------
# Per-task envelopes
# ---------------------------------------------------------------------

@dataclass
class TaskEnvelope:
    """A bounded pre-authorisation for ONE task (see module docs)."""

    task_id: str
    category: str
    site_host: str | None = None
    max_amount: float | None = None
    currency: str | None = None
    max_actions: int | None = None
    used_amount: float = 0.0
    used_actions: int = 0
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at

    def covers(self, action: ClassifiedAction, now: float | None = None) -> bool:
        """Pure coverage check (no consumption)."""
        if self.is_expired(now):
            return False
        if action.category != self.category:
            return False
        if self.site_host and action.site_host and self.site_host != action.site_host:
            return False
        if self.max_actions is not None and self.used_actions >= self.max_actions:
            return False
        if self.max_amount is not None:
            if action.amount is None:
                return False  # cannot verify the spend → not covered
            if self.currency and action.currency and self.currency != action.currency:
                return False
            if self.used_amount + action.amount > self.max_amount + 1e-9:
                return False
        return True

    def consume(self, action: ClassifiedAction) -> None:
        self.used_actions += 1
        if action.amount is not None:
            self.used_amount += action.amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "site_host": self.site_host,
            "max_amount": self.max_amount,
            "currency": self.currency,
            "max_actions": self.max_actions,
            "used_amount": self.used_amount,
            "used_actions": self.used_actions,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
        }


class EnvelopeStore:
    """In-memory per-task envelope registry (engine-process state,
    like the browser session manager itself)."""

    def __init__(self) -> None:
        self._envelopes: list[TaskEnvelope] = []

    def grant(
        self,
        *,
        task_id: str,
        category: str,
        site_host: str | None = None,
        max_amount: float | None = None,
        currency: str | None = None,
        max_actions: int | None = None,
        ttl_seconds: float | None = DEFAULT_ENVELOPE_TTL_SECONDS,
        now: float | None = None,
    ) -> TaskEnvelope:
        if category == CATEGORY_PAYMENTS:
            # Payments confirm individually — an envelope can never
            # pre-authorise a payment, whoever asks (grill Q32).
            raise ValueError("Envelopes never cover payments: payments confirm individually.")
        if category not in CATEGORIES:
            raise ValueError(f"Unknown envelope category: {category!r}.")
        if not task_id:
            raise ValueError("An envelope needs a task to scope to.")
        granted_at = time.time() if now is None else now
        envelope = TaskEnvelope(
            task_id=task_id,
            category=category,
            site_host=site_host,
            max_amount=max_amount,
            currency=currency,
            max_actions=max_actions,
            granted_at=granted_at,
            expires_at=(granted_at + ttl_seconds) if ttl_seconds else None,
        )
        self._envelopes.append(envelope)
        return envelope

    def check_and_consume(
        self,
        *,
        task_id: str | None,
        action: ClassifiedAction,
        now: float | None = None,
    ) -> TaskEnvelope | None:
        """The envelope covering *action* for *task_id*, consumed —
        or None. A failed check consumes nothing."""
        if not task_id:
            return None
        self._prune(now)
        for envelope in self._envelopes:
            if envelope.task_id != task_id:
                continue
            if envelope.covers(action, now=now):
                envelope.consume(action)
                return envelope
        return None

    def revoke_task(self, task_id: str) -> int:
        before = len(self._envelopes)
        self._envelopes = [e for e in self._envelopes if e.task_id != task_id]
        return before - len(self._envelopes)

    def list_for_task(self, task_id: str) -> list[TaskEnvelope]:
        self._prune()
        return [e for e in self._envelopes if e.task_id == task_id]

    def _prune(self, now: float | None = None) -> None:
        self._envelopes = [e for e in self._envelopes if not e.is_expired(now)]


_envelope_store: EnvelopeStore | None = None


def get_envelope_store() -> EnvelopeStore:
    global _envelope_store
    if _envelope_store is None:
        _envelope_store = EnvelopeStore()
    return _envelope_store


def reset_envelope_store() -> None:
    """Drop the process-wide store (tests; engine shutdown)."""
    global _envelope_store
    _envelope_store = None


# ---------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class GateDecision:
    verdict: str  # "allow" | "confirm" | "deny"
    reason: str
    classification: ClassifiedAction
    envelope: TaskEnvelope | None = None


def evaluate_browser_action(
    policy: BrowserPolicy,
    action: ClassifiedAction,
    *,
    task_id: str | None,
    session_running: bool,
    envelope_store: EnvelopeStore | None = None,
    now: float | None = None,
) -> GateDecision:
    """Decide one classified browser action. Side-effect free EXCEPT
    for consuming a covering envelope on an envelope-allow (an
    envelope is spent by the action it authorises).

    The master switch denies only when NO browser session is live:
    flipping it off blocks new browser tasks outright but never
    kills a task that is already running mid-action (PRD §E8-6.2).
    """
    if not policy.enabled and not session_running:
        return GateDecision("deny", "browser_disabled", action)
    if action.category == CATEGORY_FREE:
        return GateDecision("allow", "free", action)
    if action.category == CATEGORY_UNKNOWN:
        return GateDecision("confirm", "unclassified", action)
    mode = policy.mode_for(action.category)
    if mode == MODE_ALLOW_FREELY:
        return GateDecision("allow", "allow_freely", action)
    if mode == MODE_ALLOW_WITH_ENVELOPE and action.category != CATEGORY_PAYMENTS:
        if not policy.envelopes_enabled:
            return GateDecision("confirm", "envelopes_disabled", action)
        store = envelope_store if envelope_store is not None else get_envelope_store()
        envelope = store.check_and_consume(task_id=task_id, action=action, now=now)
        if envelope is not None:
            return GateDecision("allow", "envelope", action, envelope=envelope)
        return GateDecision("confirm", "envelope_not_covering", action)
    # always_confirm — and allow_with_envelope for payments, which
    # envelopes never cover: payments confirm individually.
    return GateDecision("confirm", "always_confirm", action)


def grant_envelope_from_confirmation(
    *,
    task_id: str | None,
    action: ClassifiedAction,
    spec: Any,
    envelope_store: EnvelopeStore | None = None,
) -> TaskEnvelope | None:
    """Register the envelope an owner granted while confirming.

    The grant's category and site ALWAYS come from the confirmed
    action itself — the client spec only opts in and may narrow the
    bounds (action-count cap); it can never widen the scope to a
    category or site the owner did not just confirm. Payments and
    unknown actions can never seed an envelope.
    """
    if not isinstance(spec, dict) or not spec.get("grant"):
        return None
    if action.category in (CATEGORY_PAYMENTS, CATEGORY_FREE, CATEGORY_UNKNOWN):
        return None
    if not task_id:
        return None
    max_actions = spec.get("max_actions")
    if not isinstance(max_actions, int) or isinstance(max_actions, bool) \
            or max_actions <= 0:
        max_actions = DEFAULT_ENVELOPE_MAX_ACTIONS
    max_actions = min(max_actions, DEFAULT_ENVELOPE_MAX_ACTIONS)
    store = envelope_store if envelope_store is not None else get_envelope_store()
    return store.grant(
        task_id=task_id,
        category=action.category,
        site_host=action.site_host or None,
        max_actions=max_actions,
    )
