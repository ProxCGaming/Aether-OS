"""Dead-model pin guard (15 Nov 2026).

A stale ``agent_models`` pin to gemini-2.5-flash killed a task on the
owner's machine: routing excludes that model by name
(``KNOWN_UNUSABLE_MODELS``), but pins bypass routing by design, the
pinned provider constructed fine, and its first call 404'd (Google
retired the model) — failing the whole run after the planner had
already produced output. The resolvers now refuse a pin that names a
known-unusable model and fall back to the run's default provider —
the model the owner selected in Settings — never a hardcoded one.
"""

from pathlib import Path

from aether_engine.langgraph.model_routing import pin_targets_unusable_model
from aether_engine.langgraph.nodes.supervisor import _resolve_supervisor_provider

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeProvider:
    """Stands in for the run's default provider (call_stream is the
    attribute the resolvers check)."""

    async def call_stream(self, *args, **kwargs):  # pragma: no cover
        yield None


def _config(agent_models):
    return {
        "configurable": {
            "provider": _FakeProvider(),
            "agent_models": agent_models,
            "secret_store": object(),
        }
    }


# ── The helper ──────────────────────────────────────────────────────


def test_pin_with_provider_prefix_is_caught():
    assert pin_targets_unusable_model("google_gemini:gemini-2.5-flash") is True


def test_pin_with_litellm_style_model_path_is_caught():
    assert pin_targets_unusable_model("google_gemini:gemini/gemini-2.5-flash") is True


def test_bare_model_name_is_caught():
    assert pin_targets_unusable_model("gemini-2.5-flash") is True


def test_live_model_pin_is_allowed():
    assert pin_targets_unusable_model("google_gemini:gemini-3.8-flash") is False


def test_inherit_and_empty_pins_are_allowed():
    assert pin_targets_unusable_model("inherit") is False
    assert pin_targets_unusable_model(None) is False
    assert pin_targets_unusable_model("") is False


# ── The supervisor resolver falls back to the default provider ─────


def test_supervisor_dead_pin_falls_back_to_default_provider():
    config = _config({"supervisor": "google_gemini:gemini-2.5-flash"})
    provider = _resolve_supervisor_provider(config)
    assert isinstance(provider, _FakeProvider)


def test_supervisor_without_pin_uses_default_provider():
    config = _config({})
    provider = _resolve_supervisor_provider(config)
    assert isinstance(provider, _FakeProvider)


# ── The specialist runner carries the same guard ────────────────────


def test_runner_consults_the_guard_before_constructing_a_pin():
    """The runner's resolution is embedded in the node factory, so
    its wiring is pinned textually (the house style for wiring
    contracts): the guard must be consulted, and before the pinned
    provider is constructed."""
    text = (REPO_ROOT / "aether_engine/langgraph/nodes/runner.py").read_text(
        encoding="utf-8"
    )
    guard_at = text.index("pin_targets_unusable_model(agent_model_override)")
    construct_at = text.index("_create_provider_instance")
    assert guard_at < construct_at
