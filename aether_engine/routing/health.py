"""Provider health monitoring, non-blocking latency tracking, and cooldown management."""
from dataclasses import asdict, dataclass
import time
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ProviderHealthState:
    provider: str
    status: str = "healthy"  # "healthy", "degraded", "offline"
    last_successful_request: Optional[float] = None
    latency_ms: float = 0.0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: Optional[str] = None

    def is_available(self, now: Optional[float] = None) -> bool:
        current_time = now if now is not None else time.time()
        if self.status == "offline":
            # If offline due to temporary failures and cooldown expired, allow a probe attempt
            if self.consecutive_failures > 0 and self.consecutive_failures < 5:
                return current_time >= self.cooldown_until
            return False
        if self.status == "degraded":
            return current_time >= self.cooldown_until
        return True


class ProviderHealthManager:
    """Tracks runtime health, latency metrics, and failure cooldowns for all providers."""

    def __init__(self, on_status_change: Optional[Callable[[str, ProviderHealthState], None]] = None):
        self._states: Dict[str, ProviderHealthState] = {}
        self.on_status_change = on_status_change

    def get_state(self, provider: str) -> ProviderHealthState:
        if provider not in self._states:
            self._states[provider] = ProviderHealthState(provider=provider)
        return self._states[provider]

    def is_provider_available(self, provider: str) -> bool:
        return self.get_state(provider).is_available()

    def record_success(self, provider: str, latency_ms: float) -> ProviderHealthState:
        state = self.get_state(provider)
        prev_status = state.status
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
        prev_status = state.status
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
        state.status = "healthy"
        state.consecutive_failures = 0
        state.cooldown_until = 0.0
        state.last_error = None
        if self.on_status_change:
            self.on_status_change(provider, state)
        return state

    def get_all_statuses(self) -> Dict[str, Dict[str, Any]]:
        return {p: asdict(s) for p, s in self._states.items()}


GLOBAL_HEALTH_MANAGER = ProviderHealthManager()
