"""Epic 6 — desktop standard: engine-side pins.

E6-02: onboarding_completed round-trips through UserConfig and is a
project-forbidden key (a repo's project config must not mark a user
as onboarded).

E6-06: a fresh UserConfig IS the reset target — the reset handler
replaces the config wholesale, so the defaults must be complete and
self-consistent (this pins the default surface the reset restores).
"""

from __future__ import annotations

from aether_engine.config import UserConfig
from aether_engine.config_layers import PROJECT_FORBIDDEN_KEYS


def test_onboarding_flag_round_trips():
    cfg = UserConfig()
    assert cfg.onboarding_completed is False
    cfg.onboarding_completed = True
    restored = UserConfig.from_dict(cfg.to_dict())
    assert restored.onboarding_completed is True


def test_onboarding_flag_is_project_forbidden():
    assert "onboarding_completed" in PROJECT_FORBIDDEN_KEYS


def test_reset_defaults_are_complete():
    fresh = UserConfig()
    d = fresh.to_dict()
    # The keys the desktop depends on after a reset.
    for key in (
        "default_provider",
        "default_model",
        "workspace_roots",
        "agent_models",
        "approval_mode",
        "memory_backend",
        "onboarding_completed",
    ):
        assert key in d, key
    assert d["onboarding_completed"] is False
    assert d["memory_backend"] == "native"
    assert d["default_model"] == "gemini-3.5-flash"
