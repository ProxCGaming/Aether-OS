"""Epic 8, E8-6.2: browser confirmation system + Settings policy surface.

Behaviour pinned (grill Q32–Q35 + PRD §E8-6.2):

1. Consequential browser actions are classified into a taxonomy —
   payments, sends/publishes, deletes, account changes. Browsing,
   reading, searching and filling ordinary form fields are FREE.
   An action the classifier cannot prove free or consequential is
   "unknown" and ALWAYS confirms (fail-closed): no path lets an
   unclassified action proceed silently.
2. Each category has an owner-controlled mode in Settings — Always
   confirm / Allow-with-envelope / Allow freely. The shipped preset
   is Always confirm for every category, envelopes enabled, the
   browser agent enabled, vault autofill off.
3. In Always-confirm mode the action stops on a confirmation card
   naming the EXACT action ("Pay ₹499 to Merchant") — the amount and
   merchant come from the page, never invented. Approval proceeds;
   rejection blocks the action.
4. A per-task envelope is a bounded pre-authorisation (one task, one
   category, one site, an action-count and/or spend bound, an
   expiry). An in-envelope action proceeds silently; the first
   over-envelope action stops. Envelopes never cover payments —
   payments confirm individually. Envelopes are scoped per task:
   one task's envelope never covers another task's actions.
5. The global browser-agent toggle blocks NEW browser tasks
   outright when off — but never kills a task that is already
   running mid-action.
6. Every gate decision is audited (auto-allow, confirmation
   required/approved/denied, envelope granted, disabled block) —
   with hosts and categories, never typed text or full URLs.

Playwright stays faked (the FakeDriver pattern from
tests/test_browser_core.py); the classifier works on the session's
cached page snapshot, so no browser launches here either.
"""
import asyncio
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from aether_common.contracts import EventType
from aether_engine.audit import AuditLogger
from aether_engine.browser.policy import (
    BrowserPolicy,
    EnvelopeStore,
    classify_browser_action,
    evaluate_browser_action,
    get_envelope_store,
    policy_from_user_config,
    reset_envelope_store,
    set_browser_policy_loader,
)
from aether_engine.browser.session import (
    BrowserError,
    BrowserSessionManager,
)
from aether_engine.config import UserConfig

# ---------------------------------------------------------------------
# Fixture pages
# ---------------------------------------------------------------------

CHECKOUT_URL = "https://shop.example.com/checkout"
CHECKOUT_PAGE = (
    "<html><head><title>Merchant</title></head><body>"
    "<h1>Checkout</h1><p>Order total: ₹499</p>"
    '<button id="pay-btn" class="pay-now">Pay ₹499</button>'
    '<button id="later-btn">Save for later</button>'
    '<a id="home-link" href="/">Home</a>'
    "</body></html>"
)
CART_URL = "https://shop.example.com/cart"
CART_PAGE = (
    "<html><head><title>Merchant — Cart</title></head><body>"
    "<h1>Your cart</h1>"
    '<button id="add-btn">Add to cart</button>'
    '<button id="checkout-btn">Proceed to checkout</button>'
    "</body></html>"
)
MAIL_URL = "https://mail.example.com/inbox"
MAIL_PAGE = (
    "<html><head><title>Webmail</title></head><body>"
    '<textarea id="compose" name="body" placeholder="Write a message"></textarea>'
    '<button id="send-btn">Send message</button>'
    '<button id="delete-btn">Delete</button>'
    '<input id="search-box" type="search" name="q" placeholder="Search mail">'
    "</body></html>"
)
SOCIAL_URL = "https://social.example.com/home"
SOCIAL_PAGE = (
    "<html><head><title>Social</title></head><body>"
    '<button id="publish-btn">Publish post</button>'
    "</body></html>"
)
ACCOUNT_URL = "https://accounts.example.com/settings"
ACCOUNT_PAGE = (
    "<html><head><title>Account settings</title></head><body>"
    '<button id="pw-btn">Change password</button>'
    '<button id="save-btn">Save changes</button>'
    '<input id="card-field" name="cc-number" autocomplete="cc-number" placeholder="Card number">'
    '<input id="pw-field" type="password" name="password" autocomplete="current-password">'
    "</body></html>"
)
PLAIN_URL = "https://example.com/"
PLAIN_PAGE = (
    "<html><head><title>Example Domain</title></head><body>"
    "<h1>Example Domain</h1>"
    '<a id="more-link" href="/about">Learn more</a>'
    '<button id="mystery-btn"></button>'
    '<button id="submit-btn">Submit</button>'
    "</body></html>"
)


class FakeDriver:
    """In-memory driver (same duck-typed surface as 6.1's tests)."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls = []
        self.closed = False
        self._url = "about:blank"
        self._title = ""
        self._html = ""

    async def goto(self, url):
        self.calls.append(("goto", url))
        title, html = self.pages.get(url, ("", "<html><body></body></html>"))
        self._url, self._title, self._html = url, title, html

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, text):
        self.calls.append(("fill", selector, text))

    async def extract_text(self, selector=None):
        import re

        return re.sub(r"<[^>]+>", " ", self._html)

    async def current_url(self):
        return self._url

    async def page_title(self):
        return self._title

    async def page_content(self):
        return self._html

    async def close(self):
        self.calls.append(("close",))
        self.closed = True


def make_manager(tmp_path, driver):
    async def factory(_profile_dir):
        return driver

    return BrowserSessionManager(
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        driver_factory=factory,
    )


@pytest.fixture(autouse=True)
def _reset_policy_state():
    reset_envelope_store()
    set_browser_policy_loader(None)
    yield
    reset_envelope_store()
    set_browser_policy_loader(None)


# ---------------------------------------------------------------------
# Policy config: shipped preset + UserConfig plumbing
# ---------------------------------------------------------------------

class TestPolicyConfig:
    def test_shipped_preset_is_always_confirm_everywhere(self):
        policy = BrowserPolicy()
        assert policy.enabled is True
        assert policy.envelopes_enabled is True
        assert policy.vault_autofill is False
        for category in ("payments", "sends_publishes", "deletes", "account_changes"):
            assert policy.mode_for(category) == "always_confirm", category

    def test_user_config_defaults_carry_the_preset(self):
        policy = policy_from_user_config(UserConfig())
        assert policy.to_dict() == BrowserPolicy().to_dict()

    def test_user_config_round_trip(self):
        cfg = UserConfig()
        cfg.browser_agent_enabled = False
        cfg.browser_envelopes_enabled = False
        cfg.browser_vault_autofill = True
        cfg.browser_category_modes = {
            "payments": "always_confirm",
            "sends_publishes": "allow_freely",
            "deletes": "allow_with_envelope",
            "account_changes": "always_confirm",
        }
        restored = UserConfig.from_dict(cfg.to_dict())
        policy = policy_from_user_config(restored)
        assert policy.enabled is False
        assert policy.envelopes_enabled is False
        assert policy.vault_autofill is True
        assert policy.mode_for("sends_publishes") == "allow_freely"
        assert policy.mode_for("deletes") == "allow_with_envelope"

    def test_invalid_mode_in_config_falls_back_to_always_confirm(self):
        cfg = UserConfig.from_dict({
            "browser_category_modes": {"payments": "yolo", "deletes": "allow_freely"},
        })
        policy = policy_from_user_config(cfg)
        assert policy.mode_for("payments") == "always_confirm"
        assert policy.mode_for("deletes") == "allow_freely"
        # Unknown categories are dropped, never honoured.
        assert "yolo-category" not in policy.categories

    def test_policy_dict_matches_the_ws_contract_shape(self):
        payload = BrowserPolicy().to_dict()
        assert set(payload) == {"enabled", "vault_autofill", "envelopes_enabled", "categories"}
        assert set(payload["categories"]) == {
            "payments", "sends_publishes", "deletes", "account_changes"}


# ---------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------

def classify(tool, args, url="", title="", content=""):
    return classify_browser_action(tool, args, url=url, title=title, content=content)


class TestClassifierFreeActions:
    def test_navigate_is_free(self):
        action = classify("browser_navigate", {"url": CHECKOUT_URL})
        assert action.category == "free"

    def test_extract_is_free(self):
        action = classify("browser_extract", {}, url=PLAIN_URL,
                          title="Example Domain", content=PLAIN_PAGE)
        assert action.category == "free"

    def test_ordinary_link_click_is_free(self):
        action = classify("browser_click", {"selector": "#home-link"},
                          url=CHECKOUT_URL, title="Merchant", content=CHECKOUT_PAGE)
        assert action.category == "free"

    def test_add_to_cart_is_free(self):
        action = classify("browser_click", {"selector": "#add-btn"},
                          url=CART_URL, title="Merchant — Cart", content=CART_PAGE)
        assert action.category == "free"

    def test_typing_into_a_search_box_is_free(self):
        action = classify("browser_type", {"selector": "#search-box", "text": "invoices"},
                          url=MAIL_URL, title="Webmail", content=MAIL_PAGE)
        assert action.category == "free"

    def test_typing_into_an_ordinary_textarea_is_free(self):
        action = classify("browser_type", {"selector": "#compose", "text": "hello"},
                          url=MAIL_URL, title="Webmail", content=MAIL_PAGE)
        assert action.category == "free"


class TestClassifierConsequential:
    def test_pay_button_is_a_payment_with_the_exact_action(self):
        action = classify("browser_click", {"selector": "#pay-btn"},
                          url=CHECKOUT_URL, title="Merchant", content=CHECKOUT_PAGE)
        assert action.category == "payments"
        assert action.description == "Pay ₹499 to Merchant"
        assert action.amount == 499.0
        assert action.currency == "INR"
        assert action.site_host == "shop.example.com"

    def test_proceed_to_checkout_is_a_payment_step(self):
        action = classify("browser_click", {"selector": "#checkout-btn"},
                          url=CART_URL, title="Merchant — Cart", content=CART_PAGE)
        assert action.category == "payments"

    def test_amount_is_never_invented(self):
        page = ("<html><head><title>Merchant</title></head><body>"
                '<button id="pay-btn">Pay now</button></body></html>')
        action = classify("browser_click", {"selector": "#pay-btn"},
                          url=CHECKOUT_URL, title="Merchant", content=page)
        assert action.category == "payments"
        assert action.amount is None
        assert "₹" not in action.description and "$" not in action.description

    def test_send_message_is_sends_publishes(self):
        action = classify("browser_click", {"selector": "#send-btn"},
                          url=MAIL_URL, title="Webmail", content=MAIL_PAGE)
        assert action.category == "sends_publishes"
        assert "Send message" in action.description

    def test_publish_is_sends_publishes(self):
        action = classify("browser_click", {"selector": "#publish-btn"},
                          url=SOCIAL_URL, title="Social", content=SOCIAL_PAGE)
        assert action.category == "sends_publishes"

    def test_delete_button_is_deletes(self):
        action = classify("browser_click", {"selector": "#delete-btn"},
                          url=MAIL_URL, title="Webmail", content=MAIL_PAGE)
        assert action.category == "deletes"

    def test_change_password_is_account_changes(self):
        action = classify("browser_click", {"selector": "#pw-btn"},
                          url=ACCOUNT_URL, title="Account settings", content=ACCOUNT_PAGE)
        assert action.category == "account_changes"

    def test_save_changes_on_a_settings_page_is_account_changes(self):
        action = classify("browser_click", {"selector": "#save-btn"},
                          url=ACCOUNT_URL, title="Account settings", content=ACCOUNT_PAGE)
        assert action.category == "account_changes"

    def test_typing_card_details_is_payments(self):
        action = classify("browser_type", {"selector": "#card-field", "text": "4242"},
                          url=ACCOUNT_URL, title="Account settings", content=ACCOUNT_PAGE)
        assert action.category == "payments"
        # The typed text never leaks into the description / card.
        assert "4242" not in action.description


class TestClassifierFailClosed:
    def test_typing_into_a_password_field_is_not_free(self):
        action = classify("browser_type", {"selector": "#pw-field", "text": "hunter2"},
                          url=ACCOUNT_URL, title="Account settings", content=ACCOUNT_PAGE)
        assert action.category == "unknown"
        assert "hunter2" not in action.description

    def test_opaque_click_with_no_page_evidence_is_unknown(self):
        action = classify("browser_click", {"selector": "#mystery-btn"},
                          url=PLAIN_URL, title="Example Domain", content=PLAIN_PAGE)
        assert action.category == "unknown"

    def test_click_without_any_page_context_is_unknown(self):
        action = classify("browser_click", {"selector": "div > div:nth-child(3)"})
        assert action.category == "unknown"

    def test_bare_submit_is_unknown(self):
        action = classify("browser_click", {"selector": "#submit-btn"},
                          url=PLAIN_URL, title="Example Domain", content=PLAIN_PAGE)
        assert action.category == "unknown"

    def test_unidentifiable_type_target_is_unknown(self):
        action = classify("browser_type", {"selector": "div > span:nth-child(2)", "text": "x"})
        assert action.category == "unknown"


# ---------------------------------------------------------------------
# Envelope store
# ---------------------------------------------------------------------

def _send_action(host="mail.example.com"):
    return classify("browser_click", {"selector": "#send-btn"},
                    url=MAIL_URL, title="Webmail", content=MAIL_PAGE)


class TestEnvelopeStore:
    def test_grant_and_consume_within_bounds(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=2)
        action = _send_action()
        assert store.check_and_consume(task_id="t1", action=action) is not None
        assert store.check_and_consume(task_id="t1", action=action) is not None

    def test_at_the_limit_the_next_action_is_not_covered(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=2)
        action = _send_action()
        assert store.check_and_consume(task_id="t1", action=action) is not None
        assert store.check_and_consume(task_id="t1", action=action) is not None
        # Third action exceeds the granted count: not covered, and a
        # failed check consumes nothing.
        assert store.check_and_consume(task_id="t1", action=action) is None
        assert store.check_and_consume(task_id="t1", action=action) is None

    def test_spend_cap_boundary_for_a_capped_category(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_amount=100.0, currency="INR")
        priced = replace(_send_action(), amount=100.0, currency="INR")
        # Exactly at the cap: covered.
        assert store.check_and_consume(task_id="t1", action=priced) is not None
        # Budget fully spent: even a tiny further amount is not covered.
        priced = replace(_send_action(), amount=0.5, currency="INR")
        assert store.check_and_consume(task_id="t1", action=priced) is None

    def test_scope_isolation_between_tasks(self):
        store = EnvelopeStore()
        store.grant(task_id="task-a", category="sends_publishes",
                    site_host="mail.example.com", max_actions=5)
        action = _send_action()
        assert store.check_and_consume(task_id="task-b", action=action) is None
        assert store.check_and_consume(task_id="task-a", action=action) is not None

    def test_scope_isolation_between_sites(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=5)
        action = replace(_send_action(), site_host="other.example.com")
        assert store.check_and_consume(task_id="t1", action=action) is None

    def test_expired_envelope_covers_nothing(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=5, ttl_seconds=60)
        action = _send_action()
        future = time.time() + 3600
        assert store.check_and_consume(task_id="t1", action=action, now=future) is None

    def test_envelopes_never_cover_payments(self):
        store = EnvelopeStore()
        with pytest.raises(ValueError, match="payments"):
            store.grant(task_id="t1", category="payments",
                        site_host="shop.example.com", max_amount=1000.0, currency="INR")

    def test_revoke_task_clears_its_envelopes(self):
        store = EnvelopeStore()
        store.grant(task_id="t1", category="deletes",
                    site_host="mail.example.com", max_actions=3)
        store.revoke_task("t1")
        action = classify("browser_click", {"selector": "#delete-btn"},
                          url=MAIL_URL, title="Webmail", content=MAIL_PAGE)
        assert store.check_and_consume(task_id="t1", action=action) is None


# ---------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------

def _policy(**overrides):
    base = {
        "payments": "always_confirm",
        "sends_publishes": "always_confirm",
        "deletes": "always_confirm",
        "account_changes": "always_confirm",
    }
    base.update(overrides.pop("categories", {}))
    return BrowserPolicy(categories=base, **overrides)


def _pay_action():
    return classify("browser_click", {"selector": "#pay-btn"},
                    url=CHECKOUT_URL, title="Merchant", content=CHECKOUT_PAGE)


class TestGateEvaluation:
    def test_free_action_proceeds_under_the_preset(self):
        action = classify("browser_navigate", {"url": CHECKOUT_URL})
        decision = evaluate_browser_action(
            _policy(), action, task_id="t1", session_running=False)
        assert decision.verdict == "allow"

    def test_payment_confirms_under_the_preset(self):
        decision = evaluate_browser_action(
            _policy(), _pay_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_payment_confirms_even_in_envelope_mode(self):
        policy = _policy(categories={"payments": "allow_with_envelope"})
        decision = evaluate_browser_action(
            policy, _pay_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_payment_proceeds_only_when_the_owner_allows_freely(self):
        policy = _policy(categories={"payments": "allow_freely"})
        decision = evaluate_browser_action(
            policy, _pay_action(), task_id="t1", session_running=True)
        assert decision.verdict == "allow"

    def test_unknown_action_confirms_under_every_mode(self):
        action = classify("browser_click", {"selector": "#mystery-btn"},
                          url=PLAIN_URL, title="Example Domain", content=PLAIN_PAGE)
        for mode in ("always_confirm", "allow_with_envelope", "allow_freely"):
            policy = _policy(categories={
                "payments": mode, "sends_publishes": mode,
                "deletes": mode, "account_changes": mode})
            decision = evaluate_browser_action(
                policy, action, task_id="t1", session_running=True)
            assert decision.verdict == "confirm", mode

    def test_envelope_covers_in_envelope_action_only(self):
        store = get_envelope_store()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=1)
        policy = _policy(categories={"sends_publishes": "allow_with_envelope"})
        action = _send_action()
        first = evaluate_browser_action(
            policy, action, task_id="t1", session_running=True)
        assert first.verdict == "allow" and first.reason == "envelope"
        second = evaluate_browser_action(
            policy, action, task_id="t1", session_running=True)
        assert second.verdict == "confirm"

    def test_envelope_mode_without_a_grant_confirms(self):
        policy = _policy(categories={"sends_publishes": "allow_with_envelope"})
        decision = evaluate_browser_action(
            policy, _send_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_envelopes_disabled_globally_confirms(self):
        store = get_envelope_store()
        store.grant(task_id="t1", category="sends_publishes",
                    site_host="mail.example.com", max_actions=5)
        policy = _policy(envelopes_enabled=False,
                         categories={"sends_publishes": "allow_with_envelope"})
        decision = evaluate_browser_action(
            policy, _send_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_disabled_blocks_a_new_browser_task_outright(self):
        policy = _policy(enabled=False)
        nav = evaluate_browser_action(
            policy, classify("browser_navigate", {"url": CHECKOUT_URL}),
            task_id="t-new", session_running=False)
        assert nav.verdict == "deny"
        pay = evaluate_browser_action(
            policy, _pay_action(), task_id="t-new", session_running=False)
        assert pay.verdict == "deny"

    def test_disabled_does_not_kill_a_running_task(self):
        policy = _policy(enabled=False)
        nav = evaluate_browser_action(
            policy, classify("browser_navigate", {"url": CHECKOUT_URL}),
            task_id="t-running", session_running=True)
        assert nav.verdict == "allow"
        pay = evaluate_browser_action(
            policy, _pay_action(), task_id="t-running", session_running=True)
        assert pay.verdict == "confirm"


# ---------------------------------------------------------------------
# Session layer: the master switch blocks new sessions only
# ---------------------------------------------------------------------

class TestSessionMasterSwitch:
    async def test_new_session_blocked_while_disabled(self, tmp_path):
        set_browser_policy_loader(lambda: BrowserPolicy(enabled=False))
        manager = make_manager(tmp_path, FakeDriver())
        with pytest.raises(BrowserError, match="disabled"):
            manager.get_or_create_active()

    async def test_running_session_survives_the_toggle(self, tmp_path):
        driver = FakeDriver({PLAIN_URL: ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate(PLAIN_URL)
        set_browser_policy_loader(lambda: BrowserPolicy(enabled=False))
        # The live session is returned, not blocked or replaced.
        assert manager.get_or_create_active() is session
        await session.navigate(PLAIN_URL)


# ---------------------------------------------------------------------
# Pause-node gate integration
# ---------------------------------------------------------------------

def _audit_logger(tmp_path):
    return AuditLogger(log_path=tmp_path / "audit.log")


def _pause_config(user_config, audit_logger, request_id="req-1"):
    return {"configurable": {
        "request_id": request_id,
        "user_config": user_config,
        "audit_logger": audit_logger,
    }}


def _pending(name, args):
    return {"pending_tool_call": {"name": name, "args": args, "call_id": "call-1"},
            "task_id": "task-1"}


class TestPauseNodeGate:
    def _manager_on(self, tmp_path, pages, start_url):
        driver = FakeDriver(pages)
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-1")
        asyncio.run(session.navigate(start_url))
        return manager, session, driver

    def test_non_browser_tool_uses_the_generic_pause(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        with patch("aether_engine.langgraph.nodes.pause.interrupt",
                   return_value={"approved": True}) as mock_interrupt:
            result = dummy_pause_node(
                _pending("write_file", {"path": "/tmp/x"}),
                _pause_config(UserConfig(), _audit_logger(tmp_path)))
        assert result["pending_tool_call"]["approved"] is True
        payload = mock_interrupt.call_args[0][0]
        assert "browser_confirmation" not in payload

    def test_payment_stops_with_the_exact_action_card(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        manager, _session, _driver = self._manager_on(
            tmp_path, {CHECKOUT_URL: ("Merchant", CHECKOUT_PAGE)}, CHECKOUT_URL)
        with patch("aether_engine.browser.session._manager", manager), \
                patch("aether_engine.langgraph.nodes.pause.interrupt",
                      return_value={"approved": True}) as mock_interrupt:
            result = dummy_pause_node(
                _pending("browser_click", {"selector": "#pay-btn"}),
                _pause_config(UserConfig(), _audit_logger(tmp_path)))
        payload = mock_interrupt.call_args[0][0]
        confirmation = payload["browser_confirmation"]
        assert confirmation["category"] == "payments"
        assert confirmation["description"] == "Pay ₹499 to Merchant"
        assert confirmation["amount"] == 499.0
        assert result["pending_tool_call"]["approved"] is True
        audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "BROWSER_CONFIRMATION_APPROVED" in audit_text

    def test_rejected_payment_never_proceeds(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        manager, _session, _driver = self._manager_on(
            tmp_path, {CHECKOUT_URL: ("Merchant", CHECKOUT_PAGE)}, CHECKOUT_URL)
        with patch("aether_engine.browser.session._manager", manager), \
                patch("aether_engine.langgraph.nodes.pause.interrupt",
                      return_value={"approved": False}):
            result = dummy_pause_node(
                _pending("browser_click", {"selector": "#pay-btn"}),
                _pause_config(UserConfig(), _audit_logger(tmp_path)))
        assert result["pending_tool_call"] is None
        message = result["messages"][0]
        assert "rejected" in message["content"].lower()
        audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "BROWSER_CONFIRMATION_DENIED" in audit_text

    def test_free_browser_action_skips_the_pause(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        manager, _session, _driver = self._manager_on(
            tmp_path, {CHECKOUT_URL: ("Merchant", CHECKOUT_PAGE)}, CHECKOUT_URL)
        with patch("aether_engine.browser.session._manager", manager), \
                patch("aether_engine.langgraph.nodes.pause.interrupt") as mock_interrupt:
            result = dummy_pause_node(
                _pending("browser_click", {"selector": "#home-link"}),
                _pause_config(UserConfig(), _audit_logger(tmp_path)))
        mock_interrupt.assert_not_called()
        assert result["pending_tool_call"]["approved"] is True
        audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "BROWSER_ACTION_AUTO_ALLOWED" in audit_text

    def test_envelope_grant_at_confirmation_covers_the_next_action(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        cfg = UserConfig()
        cfg.browser_category_modes = {
            "payments": "always_confirm",
            "sends_publishes": "allow_with_envelope",
            "deletes": "always_confirm",
            "account_changes": "always_confirm",
        }
        manager, _session, _driver = self._manager_on(
            tmp_path,
            {MAIL_URL: ("Webmail", MAIL_PAGE), CHECKOUT_URL: ("Merchant", CHECKOUT_PAGE)},
            MAIL_URL)
        audit = _audit_logger(tmp_path)
        with patch("aether_engine.browser.session._manager", manager), \
                patch("aether_engine.langgraph.nodes.pause.interrupt",
                      return_value={"approved": True,
                                    "browser_envelope": {"grant": True}}) as mock_interrupt:
            first = dummy_pause_node(
                _pending("browser_click", {"selector": "#send-btn"}),
                _pause_config(cfg, audit))
            assert first["pending_tool_call"]["approved"] is True
            assert mock_interrupt.call_count == 1
            # Second send in the SAME task: covered by the envelope —
            # no pause at all.
            second = dummy_pause_node(
                _pending("browser_click", {"selector": "#send-btn"}),
                _pause_config(cfg, audit))
            assert second["pending_tool_call"]["approved"] is True
            assert mock_interrupt.call_count == 1
            # A different task is NOT covered (scope isolation).
            other_task = {"pending_tool_call": {
                "name": "browser_click", "args": {"selector": "#send-btn"},
                "call_id": "call-9"}, "task_id": "task-2"}
            mock_interrupt.return_value = {"approved": True}
            dummy_pause_node(other_task, _pause_config(cfg, audit, request_id="req-2"))
            assert mock_interrupt.call_count == 2
        audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "BROWSER_ENVELOPE_GRANTED" in audit_text

    def test_disabled_toggle_blocks_a_new_task_at_the_pause(self, tmp_path):
        from aether_engine.langgraph.nodes.pause import dummy_pause_node

        cfg = UserConfig()
        cfg.browser_agent_enabled = False
        manager = make_manager(tmp_path, FakeDriver())  # no session yet
        with patch("aether_engine.browser.session._manager", manager), \
                patch("aether_engine.langgraph.nodes.pause.interrupt") as mock_interrupt:
            result = dummy_pause_node(
                _pending("browser_navigate", {"url": CHECKOUT_URL}),
                _pause_config(cfg, _audit_logger(tmp_path)))
        mock_interrupt.assert_not_called()
        assert result["pending_tool_call"] is None
        assert "disabled" in result["messages"][0]["content"].lower()
        audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")
        assert "BROWSER_ACTION_BLOCKED_DISABLED" in audit_text


# ---------------------------------------------------------------------
# Tool router: browser writes always meet the gate
# ---------------------------------------------------------------------

class TestToolRouterBrowserBranch:
    def _route(self, user_config, name="browser_click", args=None):
        from aether_engine.langgraph.graph import tool_router
        from aether_engine.tools.registry import build_tool_registry

        state = {"pending_tool_call": {"name": name, "args": args or {"selector": "#x"}}}
        config = {"configurable": {
            "tool_registry": build_tool_registry(),
            "user_config": user_config,
        }}
        return tool_router(state, config)

    def test_browser_write_pauses_even_in_write_approval_mode(self):
        cfg = UserConfig()
        cfg.approval_mode = "write"
        assert self._route(cfg) == "pause_for_approval"

    def test_browser_write_pauses_even_when_tool_policy_allows(self):
        cfg = UserConfig()
        cfg.tool_policies = {"browser_click": "Always Allow"}
        assert self._route(cfg) == "pause_for_approval"

    def test_explicit_deny_still_goes_to_execution_for_denial(self):
        cfg = UserConfig()
        cfg.tool_policies = {"browser_click": "Deny"}
        assert self._route(cfg) == "execute_tool"

    def test_non_browser_write_in_write_mode_still_executes(self):
        cfg = UserConfig()
        cfg.approval_mode = "write"
        assert self._route(cfg, name="write_file", args={"path": "/tmp/x"}) == "execute_tool"


# ---------------------------------------------------------------------
# WebSocket policy surface (BROWSER_POLICY_GET / SET)
# ---------------------------------------------------------------------

class TestBrowserPolicyWebSocket:
    def setup_method(self):
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch

        from aether_engine.app import engine_state

        self._patch = patch
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = _P(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        engine_state.initialize_auth()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        self._saved_config = engine_state.user_config
        engine_state.user_config = UserConfig()

    def teardown_method(self):
        from aether_engine.app import engine_state

        engine_state.user_config = self._saved_config
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        from aether_engine.app import engine_state

        token = engine_state.token_path.read_text(encoding="utf-8").strip()
        return client.websocket_connect(f"/ws/tasks?token={token}")

    def _send(self, ws, event_type, payload):
        from aether_common.contracts import Event

        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_get_returns_the_shipped_preset(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.BROWSER_POLICY_GET_REQUEST, {})
            assert resp.type == EventType.BROWSER_POLICY_GET_RESPONSE
            assert resp.payload["enabled"] is True
            assert resp.payload["envelopes_enabled"] is True
            assert resp.payload["vault_autofill"] is False
            assert resp.payload["categories"]["payments"] == "always_confirm"

    def test_set_updates_partially_and_persists(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app, engine_state

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.BROWSER_POLICY_SET_REQUEST, {
                "categories": {"sends_publishes": "allow_freely"}})
            assert resp.type == EventType.BROWSER_POLICY_SET_RESPONSE
            assert resp.payload["success"] is True
            assert resp.payload["policy"]["categories"]["sends_publishes"] == "allow_freely"
            # Partial: untouched categories keep their modes.
            assert resp.payload["policy"]["categories"]["payments"] == "always_confirm"
            assert engine_state.user_config.browser_category_modes["sends_publishes"] == "allow_freely"
            resp = self._send(ws, EventType.BROWSER_POLICY_SET_REQUEST, {"enabled": False})
            assert resp.payload["policy"]["enabled"] is False
            assert engine_state.user_config.browser_agent_enabled is False
            audit_text = (self.tmp_path / "audit.log").read_text(encoding="utf-8")
            assert "BROWSER_POLICY_UPDATED" in audit_text

    def test_set_with_an_invalid_mode_is_rejected_unchanged(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app, engine_state

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.BROWSER_POLICY_SET_REQUEST, {
                "categories": {"payments": "yolo"}})
            assert resp.payload["success"] is False
            assert resp.payload["policy"]["categories"]["payments"] == "always_confirm"
            assert engine_state.user_config.browser_category_modes["payments"] == "always_confirm"
