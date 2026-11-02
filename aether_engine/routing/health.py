"""Provider health monitoring, non-blocking latency tracking, and cooldown management."""
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ProviderHealthState:
    provider: str
    # E5-F15: a state is born "standby" — created but never probed.
    # Minting "healthy" here made every unprobed/unconfigured provider
    # card advertise a health it had never earned. Only a recorded
    # outcome moves the state: success -> "healthy", failure ->
    # "degraded"/"offline". Standby is a DISPLAY state only:
    # is_available() below treats it like the old default (routable),
    # so eligibility semantics (E5-F08) are unchanged.
    status: str = "standby"  # "standby", "healthy", "degraded", "offline"
    last_successful_request: float | None = None
    latency_ms: float = 0.0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str | None = None

    def is_available(self, now: float | None = None) -> bool:
        current_time = now if now is not None else time.time()
        if self.status == "offline":
            # Once the cooldown expires, allow a probe attempt regardless of
            # how many failures accumulated (E5-F08: a former count cap made
            # the 5th transient failure permanently unprobeable, so the
            # advertised cooldown could never restore the provider). The
            # cooldown itself is the anti-hammer guard: a failed probe
            # records another failure and re-arms a fresh cooldown, a
            # successful one resets the state. This branch intentionally
            # covers nonretriable-offline states too — it always has (a
            # single auth failure was already probe-eligible after its
            # 1-hour cooldown); their far longer cooldown is the difference.
            if self.consecutive_failures > 0:
                return current_time >= self.cooldown_until
            return False
        if self.status == "degraded":
            return current_time >= self.cooldown_until
        return True


class ProviderHealthManager:
    """Tracks runtime health, latency metrics, and failure cooldowns for all providers."""

    def __init__(self, on_status_change: Callable[[str, ProviderHealthState], None] | None = None):
        self._states: dict[str, ProviderHealthState] = {}
        self.on_status_change = on_status_change

    def get_state(self, provider: str) -> ProviderHealthState:
        if provider not in self._states:
            self._states[provider] = ProviderHealthState(provider=provider)
        return self._states[provider]

    def is_provider_available(self, provider: str) -> bool:
        return self.get_state(provider).is_available()

    def record_success(self, provider: str, latency_ms: float) -> ProviderHealthState:
        state = self.get_state(provider)
        state.status = "healthy"
        state.last_successful_request = time.time()
        state.latency_ms = round(latency_ms, 2)
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.last_error = None

        if self.on_status_change:
            self.on_status_change(provider, state)
        return state

    def record_failure(
        self,
        provider: str,
        error: str,
        is_retriable: bool = True,
    ) -> ProviderHealthState:
        state = self.get_state(provider)
        state.consecutive_failures += 1
        state.last_error = error
        state.latency_ms = 0.0

        now = time.time()
        if not is_retriable:
            # Permanent / auth failure -> offline
            state.status = "offline"
            state.cooldown_until = now + 3600.0  # 1 hr until re-configured
        else:
            # Retriable / transient failure -> degraded or temporary offline
            if state.consecutive_failures >= 3:
                state.status = "offline"
                state.cooldown_until = now + min(300.0, 30.0 * state.consecutive_failures)
            else:
                state.status = "degraded"
                state.cooldown_until = now + min(60.0, 15.0 * state.consecutive_failures)

        if self.on_status_change:
            self.on_status_change(provider, state)
        return state

    def reset_provider(self, provider: str) -> ProviderHealthState:
        state = self.get_state(provider)
        # E5-F15: a reset (e.g. a freshly saved key) clears the failure
        # record but proves nothing about health — back to standby
        # until the next recorded outcome.
        state.status = "standby"
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.last_error = None
        if self.on_status_change:
            self.on_status_change(provider, state)
        return state

    def remove_provider(self, provider: str) -> None:
        """Forget all health state for a provider (used when a provider is disconnected)."""
        self._states.pop(provider, None)

    def get_all_statuses(self) -> dict[str, dict[str, Any]]:
        return {p: asdict(s) for p, s in self._states.items()}


GLOBAL_HEALTH_MANAGER = ProviderHealthManager()
