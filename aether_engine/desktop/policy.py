"""Desktop confirmation policy: taxonomy, envelopes, gate (S20).

Master build S20 (owner decision O1, 14 Nov 2026: actuation =
UIA invoke/set-value AND pixel mouse/keyboard fallback;
hard-confirm class = close/quit + system). This module is the
desktop analogue of ``aether_engine/browser/policy.py`` — the
same design, re-skinned for desktop objects (apps, windows, UI
elements) instead of sites and pages:

- **Categories** — observe / open / interact / type-into /
  clipboard / close-or-quit / system. Each has an owner-controlled
  mode (Settings → Computer Use): ``always_confirm`` /
  ``allow_with_envelope`` / ``allow_freely``.
- **Hard-confirm class** — close-or-quit and system. Like browser
  payments, they confirm individually, ALWAYS: no envelope can
  cover them, no remembered grant applies, and no Allow-freely
  setting sticks (``mode_for`` forces ``always_confirm`` and the
  config-update validation refuses to store anything looser).
- **Unknown** — an action the classifier cannot pin to a category
  confirms (fail closed). Classification reads the RESOLVED
  target — the element/window the engine's own UIA read found —
  never the model's wording (the browser's M02 rule): the model's
  locator only FINDS the element; what the action IS is read from
  the element as the OS states it.
- **Per-app rules** — an owner-edited allow/deny list keyed by
  app (process name). Deny beats everything: a denied app's
  actions are refused outright (no prompt — a prompt the user
  must keep dismissing is not a control). Allow is not a
  confirmation bypass; it only records that the app is in scope
  (apps not listed are in scope by default — the category modes
  are the posture; the list exists to carve out denials and to
  mark trusted apps explicitly).
- **Per-task envelopes** — one task, one app, one category, an
  action-count bound, a 1-hour expiry; granted ONLY from an
  approval the user gives (the confirmation card's checkbox),
  scoped to the confirmed action's own app + category.
- **Remembered grants** — the durable form ("don't ask again for
  this app + category"), stored in ``desktop/grants.py`` and
  revocable in Settings → Computer Use. They never cover the
  hard-confirm class and never override an app denial.
- **Pixel fallback** — raw mouse/keyboard at coordinates is its
  own separately-switchable class: off means pixel actions are
  refused outright; on means the FIRST pixel action of a session
  always confirms (the approval IS the session consent), and no
  envelope or remembered grant can substitute for that consent.
- **Vision grounding** — gated by the standing capture mode the
  owner set in S19 (off / ask / on_demand): off refuses, ask
  confirms every grounding, on_demand lets the action's own
  category gate decide.

The gate itself is consulted from the graph's approval pause
(``langgraph/nodes/pause.py``), exactly like the browser gate,
and re-run at execution time on the final arguments (the M03
analogue in the same module).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("aether_engine.desktop.policy")

# ---------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------

CATEGORY_OBSERVE = "observe"
CATEGORY_OPEN = "open"
CATEGORY_INTERACT = "interact"
CATEGORY_TYPE_INTO = "type_into"
CATEGORY_CLIPBOARD = "clipboard"
CATEGORY_CLOSE_OR_QUIT = "close_or_quit"
CATEGORY_SYSTEM = "system"
CATEGORIES = (
    CATEGORY_OBSERVE,
    CATEGORY_OPEN,
    CATEGORY_INTERACT,
    CATEGORY_TYPE_INTO,
    CATEGORY_CLIPBOARD,
    CATEGORY_CLOSE_OR_QUIT,
    CATEGORY_SYSTEM,
)

#: The hard-confirm class (owner decision O1): these categories
#: confirm individually, always — no envelope, no remembered
#: grant, no Allow-freely. The desktop analogue of browser
#: payments (D2-03).
HARD_CONFIRM_CATEGORIES = frozenset({
    CATEGORY_CLOSE_OR_QUIT,
    CATEGORY_SYSTEM,
})

CATEGORY_UNKNOWN = "unknown"

MODE_ALWAYS_CONFIRM = "always_confirm"
MODE_ALLOW_WITH_ENVELOPE = "allow_with_envelope"
MODE_ALLOW_FREELY = "allow_freely"
MODES = (MODE_ALWAYS_CONFIRM, MODE_ALLOW_WITH_ENVELOPE, MODE_ALLOW_FREELY)

APP_ALLOW = "allow"
APP_DENY = "deny"
APP_VERDICTS = (APP_ALLOW, APP_DENY)

#: How an action reaches the desktop. "uia" = by element through
#: UI Automation (the primary route); "pixel" = raw mouse/keyboard
#: at coordinates (the fallback class); "vision" = coordinates
#: grounded by the vision model for a UIA-blind target; "window" =
#: a window primitive (focus/close); "clipboard" = the clipboard.
VIA_UIA = "uia"
VIAS = ("uia", "pixel", "vision", "window", "clipboard")

DESKTOP_TOOL_NAMES = frozenset({"desktop_act"})

DEFAULT_ENVELOPE_TTL_SECONDS = 3600.0
DEFAULT_ENVELOPE_MAX_ACTIONS = 20

#: Vision grounding confidence floor (desktop/vision.py reports a
#: 0..1 confidence). Below it a grounded point is refused unless
#: UIA verification anchors it to a real element. A named constant
#: because it is a safety judgement, not a tuning knob.
VISION_CONFIDENCE_FLOOR = 0.6


# ---------------------------------------------------------------------
# Policy value object + UserConfig plumbing
# ---------------------------------------------------------------------

def default_category_modes() -> dict[str, str]:
    """The shipped preset: every category confirms."""
    return {category: MODE_ALWAYS_CONFIRM for category in CATEGORIES}


def clean_category_modes(raw: Any) -> dict[str, str]:
    """Validate a raw category→mode mapping (browser analogue).

    Unknown categories are dropped; unknown modes fall back to
    ``always_confirm`` — a corrupt policy must tighten, never
    loosen. Hard-confirm categories are normalised to
    ``always_confirm`` here as well as in ``mode_for``: the stored
    value is never the authority, the resolved mode is.
    """
    modes = default_category_modes()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in modes and value in MODES:
                modes[key] = value
    for category in HARD_CONFIRM_CATEGORIES:
        modes[category] = MODE_ALWAYS_CONFIRM
    return modes


def clean_app_rules(raw: Any) -> list[dict[str, str]]:
    """Validate the per-app allow/deny list.

    Entries are ``{"app": <process name>, "verdict": "allow"|"deny"}``.
    Malformed entries are dropped, duplicates collapse to the LAST
    entry for an app (an edit replaces, never stacks), matching is
    case-insensitive on the app name. A corrupt list degrades to
    fewer rules, never to a wider scope: dropping a deny entry
    would loosen, so — mirror of the modes rule — when in doubt
    the entry is kept only if fully well-formed.
    """
    rules: dict[str, dict[str, str]] = {}
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            app = str(entry.get("app") or "").strip()
            verdict = str(entry.get("verdict") or "").strip().lower()
            if app and verdict in APP_VERDICTS:
                rules[app.lower()] = {"app": app, "verdict": verdict}
    return list(rules.values())


@dataclass(frozen=True)
class DesktopPolicy:
    """The owner's desktop-control policy (Settings → Computer Use).

    ``enabled`` here is the MASTER switch only; whether control
    may actually run is :func:`control_permitted` (master AND
    control AND the one-time acknowledgement) — the policy object
    carries all three so the gate reads one value object.
    """

    enabled: bool = False
    control: bool = False
    control_ack: bool = False
    envelopes_enabled: bool = True
    pixel_fallback: bool = False
    vision_capture: str = "off"
    categories: dict[str, str] = field(default_factory=default_category_modes)
    app_rules: list[dict[str, str]] = field(default_factory=list)

    @property
    def control_permitted(self) -> bool:
        """Master AND Control AND the first-enable acknowledgement.
        Any one missing means actuation does not exist — the tools
        are not offered and a call that arrives anyway is denied."""
        return bool(self.enabled and self.control and self.control_ack)

    def mode_for(self, category: str) -> str:
        if category not in CATEGORIES:
            return MODE_ALWAYS_CONFIRM
        if category in HARD_CONFIRM_CATEGORIES:
            # O1: the hard-confirm class — re-asserted here, not
            # only in the cleaner, exactly like browser payments
            # (D2-03): a stored allow_freely for close/quit or
            # system (legacy value, hand-edited config) is never
            # honoured by any consumer of the resolved mode.
            return MODE_ALWAYS_CONFIRM
        return self.categories.get(category, MODE_ALWAYS_CONFIRM)

    def app_verdict(self, app: str | None) -> str | None:
        """The owner's verdict for one app: "deny" / "allow" / None.

        Matching is by process name, case-insensitive, exact (the
        panel lists apps by the names observe reports). Deny is
        returned whenever a deny entry matches — an app somehow
        listed twice resolves to the stricter verdict (the cleaner
        already collapses duplicates; this is the second lock).
        """
        if not app:
            return None
        wanted = str(app).strip().lower()
        if not wanted:
            return None
        verdict = None
        for rule in self.app_rules:
            if str(rule.get("app") or "").strip().lower() == wanted:
                if rule.get("verdict") == APP_DENY:
                    return APP_DENY
                verdict = APP_ALLOW
        return verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "control": bool(self.control),
            "control_ack": bool(self.control_ack),
            "control_permitted": self.control_permitted,
            "envelopes_enabled": bool(self.envelopes_enabled),
            "pixel_fallback": bool(self.pixel_fallback),
            "vision_capture": self.vision_capture,
            "categories": {c: self.mode_for(c) for c in CATEGORIES},
            "app_rules": [dict(r) for r in self.app_rules],
        }


def policy_from_user_config(user_config: Any) -> DesktopPolicy:
    """Build the policy from a UserConfig (defensive getattr reads,
    so partially-built configs in tests resolve to everything-off:
    the shipped defaults ARE off for every Computer Use switch)."""
    if user_config is None:
        return DesktopPolicy()
    vision = getattr(user_config, "computer_use_vision_capture", "off")
    if vision not in ("off", "ask", "on_demand"):
        vision = "off"
    return DesktopPolicy(
        enabled=bool(getattr(user_config, "computer_use_enabled", False)),
        control=bool(getattr(user_config, "computer_use_control", False)),
        control_ack=bool(
            getattr(user_config, "computer_use_control_ack", False)),
        envelopes_enabled=bool(
            getattr(user_config, "computer_use_envelopes_enabled", True)),
        pixel_fallback=bool(
            getattr(user_config, "computer_use_pixel_fallback", False)),
        vision_capture=vision,
        categories=clean_category_modes(
            getattr(user_config, "computer_use_category_modes", None)),
        app_rules=clean_app_rules(
            getattr(user_config, "computer_use_app_rules", None)),
    )


# ---------------------------------------------------------------------
# Policy loader seam (the engine installs the live config reader,
# mirroring browser/policy.py's loader)
# ---------------------------------------------------------------------

_policy_loader: Callable[[], DesktopPolicy] | None = None


def set_desktop_policy_loader(
    loader: Callable[[], DesktopPolicy] | None,
):
    """Install the live policy loader; returns the previous one so
    the installer can restore it (the browser loader's N-43 rule)."""
    global _policy_loader
    previous = _policy_loader
    _policy_loader = loader
    return previous


def _fail_closed_policy() -> DesktopPolicy:
    """The policy for "the owner's settings could not be read":
    everything off, every category confirming. For desktop this
    matters more than for the browser — the shipped defaults are
    already off, so falling back to them after a loader failure
    would silently re-enable nothing, but a loader failure while
    the owner HAS enabled control must never widen or narrow
    silently: off is the only honest answer."""
    return DesktopPolicy()


def current_desktop_policy() -> DesktopPolicy:
    """The live policy.

    No loader installed (tests, tools, pre-wiring): the shipped
    preset — which for Computer Use is everything OFF, so an
    unwired engine cannot actuate. A loader that fails or returns
    a non-policy: fail closed, same reasoning as the browser's.
    """
    if _policy_loader is None:
        return DesktopPolicy()
    try:
        policy = _policy_loader()
    except Exception:
        logger.debug(
            "Desktop policy loader failed; failing closed", exc_info=True)
        return _fail_closed_policy()
    return policy if isinstance(policy, DesktopPolicy) else _fail_closed_policy()


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ClassifiedDesktopAction:
    """One desktop action, classified for the gate."""

    tool_name: str
    action: str            # the desktop_act verb (invoke, set_value, …)
    category: str          # CATEGORIES member | "unknown"
    description: str       # the exact action, for the confirmation card
    app: str = ""          # owning app (process name) of the target
    via: str = VIA_UIA     # how the action reaches the desktop
    # A target the gate must never let proceed unconfirmed even
    # when its category would allow it (typing into a password
    # field). Envelopes and remembered grants never cover it.
    force_confirm: bool = False
    # A stable identity for the action's TARGET (M03 analogue):
    # the resolved element/window as the OS states it. The
    # execution-time gate compares the final action's identity
    # against the authorised one. Typed/set text is deliberately
    # NOT part of it (the browser's rule: the text is content the
    # user may edit at approval).
    identity: str = ""

    def confirmation_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "app": self.app,
            "via": self.via,
            "action": self.action,
            "tool_name": self.tool_name,
            "hard_confirm": self.category in HARD_CONFIRM_CATEGORIES,
            "force_confirm": bool(self.force_confirm),
        }


# Element wording that marks the hard-confirm classes. Classification
# reads these from the RESOLVED element as the OS states it (its
# name/role) — never from the model's locator text (M02 analogue).
_CLOSE_MARKERS = (
    "close window", "close", "quit", "exit application", "exit",
)
_SYSTEM_MARKERS = (
    "shut down", "shutdown", "restart", "sign out", "sign off",
    "log off", "log out", "lock", "sleep", "hibernate",
    "switch user", "task manager", "end task",
)
#: Roles whose invocation closes their window when the element is
#: the window's own close button: UIA reports it as a Button named
#: "Close" inside the window's title bar — the name match above is
#: what carries it; there is no separate role signal to trust.


def _element_category(name: str, role: str) -> str | None:
    """The hard-confirm category an element's own stated identity
    proves, or None. Only the two hard classes are read off element
    wording: they are the classes where a wrong call is expensive,
    and both directions fail safe (an ordinary "Close" dialog
    button confirming as close-or-quit is a prompt, not a loss)."""
    text = f"{name} {role}".strip().lower()
    if not text:
        return None
    for marker in _SYSTEM_MARKERS:
        if marker in text:
            return CATEGORY_SYSTEM
    for marker in _CLOSE_MARKERS:
        if text == marker or text.startswith(marker + " ") \
                or text.endswith(" " + marker):
            return CATEGORY_CLOSE_OR_QUIT
    return None


def _parse_key_combo(keys: str) -> tuple[bool, bool]:
    """(is_close_combo, is_system_combo) for a key_press action.

    Alt+F4 closes the foreground window — that IS close-or-quit,
    whatever the caller calls it. Combos involving the OS key
    (Win/Meta) drive the system shell — system. Ctrl+Alt+Del
    cannot be synthesised by SendInput at all (secure attention
    sequence); it classifies system and the backend refuses it
    honestly at execution.
    """
    parts = {p.strip().lower() for p in (keys or "").replace(" ", "").split("+") if p.strip()}
    if not parts:
        return False, False
    if parts == {"alt", "f4"} or parts == {"ctrl", "alt", "del"} \
            or parts == {"ctrl", "alt", "delete"}:
        return (True, False) if "alt" in parts and "f4" in parts else (False, True)
    if parts & {"win", "windows", "meta", "super", "cmd", "command"}:
        return False, True
    return False, False


def classify_desktop_action(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    resolved: Any | None = None,
) -> ClassifiedDesktopAction:
    """Classify one pending desktop action for the gate.

    ``resolved`` is the engine's OWN resolution of the target (a
    ``desktop.provider.ResolvedTarget``: the element/window the
    UIA read found for the model's locator, or the element at a
    pixel point, or the focused element) — evidence the model
    cannot fabricate. Pure and total: any doubt resolves to
    ``unknown``, never to a soft category.
    """
    args = args or {}
    verb = str(args.get("action") or "").strip().lower()

    def _action(category: str, description: str, **kw: Any) -> ClassifiedDesktopAction:
        return ClassifiedDesktopAction(
            tool_name=tool_name, action=verb, category=category,
            description=description, **kw)

    element = getattr(resolved, "element", None) if resolved is not None else None
    window = getattr(resolved, "window", None) if resolved is not None else None
    app = ""
    if window is not None:
        app = str(getattr(window, "app", "") or "")
    if element is not None and getattr(element, "app", ""):
        app = str(element.app)

    def _element_identity() -> str:
        if element is None:
            return ""
        return "element|{}|{}|{}|{}".format(
            app,
            getattr(element, "automation_id", None) or "",
            getattr(element, "name", "") or "",
            getattr(element, "role", "") or "",
        )

    if verb == "focus_window":
        label = app or str(args.get("app") or args.get("title") or "a window")
        return _action(
            CATEGORY_OPEN, f"Bring {label} to the front",
            app=app, via="window",
            identity=f"focus|{app}|{getattr(window, 'title', '') if window else ''}")
    if verb == "close_window":
        label = app or str(args.get("app") or args.get("title") or "a window")
        return _action(
            CATEGORY_CLOSE_OR_QUIT, f"Close {label}",
            app=app, via="window",
            identity=f"close|{app}|{getattr(window, 'title', '') if window else ''}")
    if verb == "invoke":
        if element is None:
            return _action(
                CATEGORY_UNKNOWN,
                "Invoke a UI element that could not be resolved "
                "on the current screen",
                app=app, identity="invoke|unresolved")
        hard = _element_category(element.name or "", element.role or "")
        label = (element.name or element.role or "element").strip()
        if hard == CATEGORY_SYSTEM:
            return _action(
                CATEGORY_SYSTEM, f"Invoke '{label}' in {app or 'the foreground app'}",
                app=app, identity=f"invoke|{_element_identity()}")
        if hard == CATEGORY_CLOSE_OR_QUIT:
            return _action(
                CATEGORY_CLOSE_OR_QUIT,
                f"Invoke '{label}' in {app or 'the foreground app'} (closes or quits)",
                app=app, identity=f"invoke|{_element_identity()}")
        return _action(
            CATEGORY_INTERACT, f"Invoke '{label}' in {app or 'the foreground app'}",
            app=app, identity=f"invoke|{_element_identity()}")
    if verb == "set_value":
        if element is None:
            return _action(
                CATEGORY_UNKNOWN,
                "Type into a UI element that could not be resolved "
                "on the current screen",
                app=app, identity="set_value|unresolved")
        label = (element.name or element.role or "field").strip()
        return _action(
            CATEGORY_TYPE_INTO, f"Type into '{label}' in {app or 'the foreground app'}",
            app=app, force_confirm=bool(getattr(element, "is_password", False)),
            identity=f"set_value|{_element_identity()}")
    if verb == "type_text":
        focused = element  # for type_text, `resolved` carries the FOCUSED element
        return _action(
            CATEGORY_TYPE_INTO,
            f"Type text into {app or 'the foreground app'}",
            app=app,
            force_confirm=bool(getattr(focused, "is_password", False)) if focused is not None else False,
            identity=f"type_text|{app}")
    if verb == "key_press":
        keys = str(args.get("keys") or "")
        is_close, is_system = _parse_key_combo(keys)
        if is_system:
            return _action(
                CATEGORY_SYSTEM, f"Press {keys} (a system shortcut)",
                app=app, via="pixel", identity=f"keys|{keys.lower()}")
        if is_close:
            return _action(
                CATEGORY_CLOSE_OR_QUIT, f"Press {keys} (closes the foreground window)",
                app=app, via="pixel", identity=f"keys|{keys.lower()}")
        return _action(
            CATEGORY_INTERACT, f"Press {keys}",
            app=app, via="pixel", identity=f"keys|{keys.lower()}")
    if verb == "click_at":
        x, y = args.get("x"), args.get("y")
        if element is not None:
            hard = _element_category(element.name or "", element.role or "")
            label = (element.name or element.role or "element").strip()
            if hard in HARD_CONFIRM_CATEGORIES:
                return _action(
                    hard, f"Click '{label}' in {app or 'the foreground app'} at ({x}, {y})",
                    app=app, via="pixel",
                    identity=f"click|{_element_identity()}")
            return _action(
                CATEGORY_INTERACT, f"Click '{label}' in {app or 'the foreground app'} at ({x}, {y})",
                app=app, via="pixel",
                identity=f"click|{_element_identity()}")
        return _action(
            CATEGORY_INTERACT, f"Click at screen coordinates ({x}, {y})",
            app=app, via="pixel", identity=f"click|point|{x},{y}")
    if verb == "ground_click":
        description = str(args.get("description") or "the described control")
        return _action(
            CATEGORY_INTERACT,
            f"Find and click '{description}' using a screenshot "
            "(accessibility could not resolve it)",
            app=app, via="vision", identity=f"ground|{description}")
    if verb == "clipboard_get":
        return _action(
            CATEGORY_CLIPBOARD, "Read the clipboard's current text",
            via="clipboard", identity="clipboard|get")
    if verb == "clipboard_set":
        return _action(
            CATEGORY_CLIPBOARD, "Replace the clipboard's contents",
            via="clipboard", identity="clipboard|set")
    return _action(
        CATEGORY_UNKNOWN,
        f"Unrecognised desktop action ({verb or 'no action given'})",
        app=app, identity=f"unknown|{verb}")


def action_matches_authorisation(record: Any, action: ClassifiedDesktopAction) -> bool:
    """Whether *action* is materially the action an authorisation
    record covers (the browser's M03 rule, desktop objects): same
    tool, same category, same target identity. Typed/set text is
    not compared — it is content the user may edit at approval."""
    if not isinstance(record, dict):
        return False
    return (
        record.get("tool_name") == action.tool_name
        and record.get("category") == action.category
        and record.get("identity", "") == action.identity
    )


# ---------------------------------------------------------------------
# Per-task envelopes (the browser's EnvelopeStore, desktop scope:
# one task, one APP, one category)
# ---------------------------------------------------------------------

@dataclass
class DesktopEnvelope:
    """A bounded pre-authorisation for ONE task (see module docs)."""

    task_id: str
    category: str
    app: str | None = None
    max_actions: int | None = None
    used_actions: int = 0
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at

    def covers(self, action: ClassifiedDesktopAction, now: float | None = None) -> bool:
        """Pure coverage check (no consumption)."""
        if self.is_expired(now):
            return False
        if action.category != self.category:
            return False
        if action.category in HARD_CONFIRM_CATEGORIES:
            return False  # defence in depth: grant() already refuses these
        if self.app and (
                # Fail closed on an unknown/empty action app, like
                # the browser's site rule: a bound that cannot be
                # verified does not cover the action.
                not action.app or self.app.lower() != action.app.lower()):
            return False
        return not (
            self.max_actions is not None
            and self.used_actions >= self.max_actions)

    def consume(self, action: ClassifiedDesktopAction) -> None:
        self.used_actions += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "app": self.app,
            "max_actions": self.max_actions,
            "used_actions": self.used_actions,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
        }


class DesktopEnvelopeStore:
    """In-memory per-task envelope registry (engine-process state,
    like the browser's envelope store)."""

    def __init__(self) -> None:
        self._envelopes: list[DesktopEnvelope] = []

    def grant(
        self,
        *,
        task_id: str,
        category: str,
        app: str | None = None,
        max_actions: int | None = None,
        ttl_seconds: float | None = DEFAULT_ENVELOPE_TTL_SECONDS,
        now: float | None = None,
    ) -> DesktopEnvelope:
        if category in HARD_CONFIRM_CATEGORIES:
            raise ValueError(
                "Envelopes never cover the hard-confirm categories "
                "(close_or_quit, system): they confirm individually.")
        if category not in CATEGORIES:
            raise ValueError(f"Unknown envelope category: {category!r}.")
        if not task_id:
            raise ValueError("An envelope needs a task to scope to.")
        granted_at = time.time() if now is None else now
        envelope = DesktopEnvelope(
            task_id=task_id,
            category=category,
            app=app,
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
        action: ClassifiedDesktopAction,
        now: float | None = None,
    ) -> DesktopEnvelope | None:
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

    def list_for_task(self, task_id: str) -> list[DesktopEnvelope]:
        self._prune()
        return [e for e in self._envelopes if e.task_id == task_id]

    def _prune(self, now: float | None = None) -> None:
        self._envelopes = [e for e in self._envelopes if not e.is_expired(now)]


_envelope_store: DesktopEnvelopeStore | None = None


def get_desktop_envelope_store() -> DesktopEnvelopeStore:
    global _envelope_store
    if _envelope_store is None:
        _envelope_store = DesktopEnvelopeStore()
    return _envelope_store


def reset_desktop_envelope_store() -> None:
    """Drop the process-wide store (tests; engine shutdown)."""
    global _envelope_store
    _envelope_store = None


# ---------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class DesktopGateDecision:
    verdict: str  # "allow" | "confirm" | "deny"
    reason: str
    classification: ClassifiedDesktopAction
    envelope: DesktopEnvelope | None = None


def evaluate_desktop_action(
    policy: DesktopPolicy,
    action: ClassifiedDesktopAction,
    *,
    task_id: str | None,
    control_refusal: str | None = None,
    pixel_consent: bool = False,
    envelope_store: DesktopEnvelopeStore | None = None,
    grant_covers: Callable[[str, str], bool] | None = None,
    now: float | None = None,
) -> DesktopGateDecision:
    """Decide one classified desktop action. Side-effect free EXCEPT
    for consuming a covering envelope on an envelope-allow.

    Order is the safety story, and it is deliberate:

    1. Control must be permitted at all (master + control + ack) —
       otherwise the action does not exist.
    2. The live control lane (paused / user-driving / this task
       stopped) refuses in place — the task continues in chat.
    3. A per-app DENY refuses outright, logged — no prompt.
    4. The hard-confirm class confirms — before envelopes, grants,
       modes, or consent of any kind.
    5. A sensitive target (password field) confirms.
    6. Pixel input: refused when the fallback switch is off; the
       session's first pixel action confirms (that approval IS the
       consent) — no envelope/grant substitutes.
    7. Vision grounding follows the standing capture mode
       (off refuses; ask confirms every time).
    8. The category mode: allow-freely allows; allow-with-envelope
       spends a covering envelope, then a remembered grant, else
       confirms; always-confirm honours a remembered grant, else
       confirms. Unknown confirms.
    """
    if not policy.control_permitted:
        return DesktopGateDecision("deny", "control_disabled", action)
    if control_refusal:
        return DesktopGateDecision("deny", control_refusal, action)
    if action.app and policy.app_verdict(action.app) == APP_DENY:
        return DesktopGateDecision("deny", "app_denied", action)
    if action.category == CATEGORY_OBSERVE:
        return DesktopGateDecision("allow", "observe", action)
    if action.category in HARD_CONFIRM_CATEGORIES:
        return DesktopGateDecision("confirm", "hard_confirm", action)
    if action.category == CATEGORY_UNKNOWN:
        return DesktopGateDecision("confirm", "unclassified", action)
    if action.force_confirm:
        return DesktopGateDecision("confirm", "sensitive_target", action)
    if action.via == "pixel":
        if not policy.pixel_fallback:
            return DesktopGateDecision("deny", "pixel_disabled", action)
        if not pixel_consent:
            return DesktopGateDecision("confirm", "pixel_session_consent", action)
    if action.via == "vision":
        if policy.vision_capture == "off":
            return DesktopGateDecision("deny", "vision_disabled", action)
        if policy.vision_capture == "ask":
            return DesktopGateDecision("confirm", "vision_ask", action)

    def _remembered() -> bool:
        return bool(
            grant_covers is not None and action.app
            and grant_covers(action.app, action.category))

    mode = policy.mode_for(action.category)
    if mode == MODE_ALLOW_FREELY:
        return DesktopGateDecision("allow", "allow_freely", action)
    if mode == MODE_ALLOW_WITH_ENVELOPE:
        if not policy.envelopes_enabled:
            if _remembered():
                return DesktopGateDecision("allow", "remembered_grant", action)
            return DesktopGateDecision("confirm", "envelopes_disabled", action)
        store = envelope_store if envelope_store is not None \
            else get_desktop_envelope_store()
        envelope = store.check_and_consume(
            task_id=task_id, action=action, now=now)
        if envelope is not None:
            return DesktopGateDecision("allow", "envelope", action, envelope=envelope)
        if _remembered():
            return DesktopGateDecision("allow", "remembered_grant", action)
        return DesktopGateDecision("confirm", "envelope_not_covering", action)
    # always_confirm
    if _remembered():
        return DesktopGateDecision("allow", "remembered_grant", action)
    return DesktopGateDecision("confirm", "always_confirm", action)


def grant_envelope_from_confirmation(
    *,
    task_id: str | None,
    action: ClassifiedDesktopAction,
    spec: Any,
    envelope_store: DesktopEnvelopeStore | None = None,
) -> DesktopEnvelope | None:
    """Register the envelope an owner granted while confirming.

    The grant's category and app ALWAYS come from the confirmed
    action itself — the client spec only opts in and may narrow
    the action-count bound; it can never widen the scope to a
    category or app the owner did not just confirm. Hard-confirm,
    observe and unknown actions can never seed an envelope.
    """
    if not isinstance(spec, dict) or not spec.get("grant"):
        return None
    if action.category in HARD_CONFIRM_CATEGORIES or action.category in (
            CATEGORY_OBSERVE, CATEGORY_UNKNOWN):
        return None
    if not task_id:
        return None
    max_actions = spec.get("max_actions")
    if not isinstance(max_actions, int) or isinstance(max_actions, bool) \
            or max_actions <= 0:
        max_actions = DEFAULT_ENVELOPE_MAX_ACTIONS
    max_actions = min(max_actions, DEFAULT_ENVELOPE_MAX_ACTIONS)
    store = envelope_store if envelope_store is not None \
        else get_desktop_envelope_store()
    return store.grant(
        task_id=task_id,
        category=action.category,
        app=action.app or None,
        max_actions=max_actions,
    )
