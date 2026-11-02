"""E5-F08 — the fifth transient health failure never becomes available
for a cooldown probe.

``ProviderHealthState.is_available()`` permitted an expired-cooldown
probe only when ``consecutive_failures < 5``, while ``record_failure()``
keeps setting a finite cooldown at >= 3 retriable failures. At 5+
failures the cooldown expired and nothing changed: availability stayed
False forever, and routing (``RoutingPolicy``), fallback selection and
startup — which all consult ``ProviderHealthManager.is_provider_available``,
this exact predicate — kept excluding the provider. Waiting out the
advertised cooldown could never restore probe eligibility on its own.

Fix: probe eligibility after cooldown expiry no longer depends on the
failure count. The cooldown itself remains the anti-hammer guard
(30s per retriable failure, capped at 300s; 1h for a nonretriable
failure): a probe failure re-arms a fresh cooldown through
``record_failure`` and a probe success resets through
``record_success``. The design has no probe-in-flight tracking and none
is added — eligibility is a pure predicate, and the next recorded
outcome re-arms or clears it.

Nonretriable note (the sharp edge): the health state records no
retriability marker for an offline provider, and ``is_available`` has
always treated every offline state through this one branch — a
nonretriable failure at counts 1-4 was ALREADY probe-eligible once its
1-hour cooldown expired. Removing the count cap extends that same
treatment uniformly to counts >= 5; the 1-hour cooldown (12-24x the
transient cap) remains the differentiator, and a failed probe against
a still-bad key records another nonretriable failure and another hour.
"""

import pytest

import aether_engine.routing.health as health_module
from aether_engine.routing.health import ProviderHealthManager

PROVIDER = "openai"


class _Clock:
    """Controllable stand-in for the ``time`` module health.py reads."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(health_module, "time", fake)
    return fake


@pytest.fixture
def manager():
    return ProviderHealthManager()


def _fail(manager, count, *, retriable=True, error="timeout"):
    for _ in range(count):
        manager.record_failure(PROVIDER, error, is_retriable=retriable)


class TestFifthFailureProbeEligibility:
    """The audit's repro: five synthetic timeouts at clock 1000 produce
    cooldown_until=1150, yet availability was False at 1151, 4600 and
    1000000."""

    def test_cooldown_is_set_as_advertised(self, clock, manager):
        _fail(manager, 5)
        state = manager.get_state(PROVIDER)
        assert state.status == "offline"
        assert state.consecutive_failures == 5
        assert state.cooldown_until == 1150.0  # 1000 + 30 * 5

    def test_availability_returns_after_cooldown_expires(self, clock, manager):
        _fail(manager, 5)
        state = manager.get_state(PROVIDER)
        assert state.is_available(now=1149.0) is False  # still cooling down
        # The audit's clock points — all False before the fix.
        assert state.is_available(now=1151.0) is True
        assert state.is_available(now=4600.0) is True
        assert state.is_available(now=1000000.0) is True

    def test_manager_predicate_policy_and_fallback_consult(self, clock, manager):
        # RoutingPolicy.select_route, fallback selection and startup all
        # gate on ProviderHealthManager.is_provider_available — this one.
        _fail(manager, 5)
        assert manager.is_provider_available(PROVIDER) is False
        clock.now = 1151.0
        assert manager.is_provider_available(PROVIDER) is True

    def test_eligibility_holds_at_the_cooldown_cap(self, clock, manager):
        _fail(manager, 12)
        state = manager.get_state(PROVIDER)
        assert state.cooldown_until == 1300.0  # backoff capped at 300s
        assert state.is_available(now=1299.0) is False
        assert state.is_available(now=1301.0) is True


class TestProbeRecoveryStory:
    """Probe-eligible is not a free pass: the next recorded outcome
    decides — success clears, failure re-arms a fresh cooldown."""

    def test_probe_success_restores_healthy(self, clock, manager):
        _fail(manager, 5)
        clock.now = 1151.0
        assert manager.is_provider_available(PROVIDER) is True
        manager.record_success(PROVIDER, latency_ms=120.0)
        state = manager.get_state(PROVIDER)
        assert state.status == "healthy"
        assert state.consecutive_failures == 0
        assert state.cooldown_until == 0.0
        assert manager.is_provider_available(PROVIDER) is True

    def test_probe_failure_rearms_a_future_cooldown(self, clock, manager):
        _fail(manager, 5)
        clock.now = 1151.0
        assert manager.is_provider_available(PROVIDER) is True
        manager.record_failure(PROVIDER, "timeout", is_retriable=True)
        state = manager.get_state(PROVIDER)
        assert state.consecutive_failures == 6
        assert state.status == "offline"
        assert state.cooldown_until == 1331.0  # 1151 + 30 * 6
        # Not immediately eligible again — the re-armed cooldown governs.
        assert state.is_available(now=1151.0) is False
        assert state.is_available(now=1330.0) is False
        assert state.is_available(now=1332.0) is True


class TestControlsUnchanged:
    """Counts and windows that already worked must behave exactly as
    before the fix."""

    @pytest.mark.parametrize("failures,cooldown_until", [(3, 1090.0), (4, 1120.0)])
    def test_counts_three_and_four_recover_as_before(
        self, clock, manager, failures, cooldown_until
    ):
        _fail(manager, failures)
        state = manager.get_state(PROVIDER)
        assert state.status == "offline"
        assert state.cooldown_until == cooldown_until
        assert state.is_available(now=cooldown_until - 1.0) is False
        assert state.is_available(now=cooldown_until + 1.0) is True

    def test_inside_cooldown_never_eligible_at_any_count(self, clock, manager):
        _fail(manager, 7)
        state = manager.get_state(PROVIDER)
        assert state.cooldown_until == 1210.0  # 1000 + 30 * 7
        assert state.is_available(now=1000.0) is False
        assert state.is_available(now=1209.0) is False

    def test_degraded_provider_unaffected(self, clock, manager):
        _fail(manager, 2)
        state = manager.get_state(PROVIDER)
        assert state.status == "degraded"
        assert state.cooldown_until == 1030.0  # 1000 + 15 * 2
        assert state.is_available(now=1029.0) is False
        assert state.is_available(now=1031.0) is True


class TestNonretriableSharpEdge:
    """Nonretriable (auth) failures take the provider offline with a
    1-hour cooldown. The state carries no retriability marker and
    is_available has always used this single branch for every offline
    state, so counts 1-4 were already probe-eligible after that hour;
    the fix makes counts >= 5 uniform with them rather than inventing
    a distinction the state cannot express."""

    def test_single_auth_failure_already_probe_eligible_after_its_hour(
        self, clock, manager
    ):
        # Pre-existing behaviour, pinned as a control: unchanged by the fix.
        manager.record_failure(PROVIDER, "401 invalid key", is_retriable=False)
        state = manager.get_state(PROVIDER)
        assert state.status == "offline"
        assert state.cooldown_until == 4600.0  # 1000 + 3600
        assert state.is_available(now=4599.0) is False
        assert state.is_available(now=4601.0) is True

    def test_auth_failure_as_fifth_failure_follows_the_same_rule(
        self, clock, manager
    ):
        _fail(manager, 4)
        manager.record_failure(PROVIDER, "401 invalid key", is_retriable=False)
        state = manager.get_state(PROVIDER)
        assert state.consecutive_failures == 5
        assert state.status == "offline"
        assert state.cooldown_until == 4600.0  # the 1-hour nonretriable cooldown
        assert state.is_available(now=4599.0) is False
        # Was False forever before the fix (count == 5), despite the
        # identical state at count 1 being eligible after its hour.
        assert state.is_available(now=4601.0) is True

    def test_failed_auth_probe_rearms_another_hour(self, clock, manager):
        manager.record_failure(PROVIDER, "401 invalid key", is_retriable=False)
        clock.now = 4601.0
        assert manager.is_provider_available(PROVIDER) is True
        manager.record_failure(PROVIDER, "401 invalid key", is_retriable=False)
        state = manager.get_state(PROVIDER)
        assert state.status == "offline"
        assert state.cooldown_until == 4601.0 + 3600.0
        assert state.is_available(now=4601.0) is False
