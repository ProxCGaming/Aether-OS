"""Wave 3, D2-03: payments ALWAYS confirm individually.

The Browser Policy panel's own note is the spec (comment-is-contract):
"Payments are always confirmed individually — envelopes never cover
payments." Before this fix the engine honoured a stored
``allow_freely`` mode for the payments category — ``mode_for``
returned it unclamped and ``evaluate_browser_action`` returned
``allow`` for it before the payments carve-out — so the one category
the panel promises can never be waived could be waived.

The clamp lives at the policy's resolution point (``mode_for``) and
is re-asserted at the gate itself, so no stored mode — and no future
consumer of either seam — can let a payment proceed without an
individual confirmation. Other categories are untouched.
"""
import pytest

from aether_engine.browser.policy import (
    BrowserPolicy,
    classify_browser_action,
    evaluate_browser_action,
    reset_envelope_store,
)

CHECKOUT_URL = "https://shop.example.com/checkout"
CHECKOUT_PAGE = (
    "<html><head><title>Merchant</title></head><body>"
    "<h1>Checkout</h1><p>Order total: ₹499</p>"
    '<button id="pay-btn" class="pay-now">Pay ₹499</button>'
    "</body></html>"
)
MAIL_URL = "https://mail.example.com/inbox"
MAIL_PAGE = (
    "<html><head><title>Webmail</title></head><body>"
    '<button id="send-btn">Send</button>'
    "</body></html>"
)


@pytest.fixture(autouse=True)
def _reset_policy_state():
    reset_envelope_store()
    yield
    reset_envelope_store()


def _policy(categories):
    base = {
        "payments": "always_confirm",
        "sends_publishes": "always_confirm",
        "deletes": "always_confirm",
        "account_changes": "always_confirm",
    }
    base.update(categories)
    return BrowserPolicy(categories=base)


def _pay_action():
    return classify_browser_action(
        "browser_click", {"selector": "#pay-btn"},
        url=CHECKOUT_URL, title="Merchant", content=CHECKOUT_PAGE)


def _send_action():
    return classify_browser_action(
        "browser_click", {"selector": "#send-btn"},
        url=MAIL_URL, title="Webmail", content=MAIL_PAGE)


class TestPaymentsClamp:
    def test_mode_for_clamps_payments_allow_freely(self):
        policy = _policy({"payments": "allow_freely"})
        assert policy.mode_for("payments") == "always_confirm"

    def test_mode_for_clamps_payments_envelope_mode(self):
        policy = _policy({"payments": "allow_with_envelope"})
        assert policy.mode_for("payments") == "always_confirm"

    def test_reported_policy_never_offers_a_waivable_payments_mode(self):
        policy = _policy({"payments": "allow_freely"})
        assert policy.to_dict()["categories"]["payments"] == "always_confirm"

    def test_payment_confirms_even_when_stored_allow_freely(self):
        policy = _policy({"payments": "allow_freely"})
        decision = evaluate_browser_action(
            policy, _pay_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_payment_confirms_from_a_raw_dict_policy(self):
        policy = BrowserPolicy.from_dict(
            {"categories": {"payments": "allow_freely"}})
        decision = evaluate_browser_action(
            policy, _pay_action(), task_id="t1", session_running=True)
        assert decision.verdict == "confirm"

    def test_other_categories_still_honour_allow_freely(self):
        # The clamp is payments-only: sends keep their stored mode.
        policy = _policy({"sends_publishes": "allow_freely"})
        assert policy.mode_for("sends_publishes") == "allow_freely"
        decision = evaluate_browser_action(
            policy, _send_action(), task_id="t1", session_running=True)
        assert decision.verdict == "allow"
