"""Aether-owned model presets and the tier policy for the
llama.cpp router.

The router itself is model-agnostic: it serves whatever GGUF
files it finds. The presets are Aether's opinionated layer on
top — named picks with a lifecycle tier each:

- ``resident``: tiny models loaded when the router starts and
  never auto-unloaded, so embeddings and instant replies never
  pay a cold load.
- ``daily``: the everyday chat model — loaded on demand and
  unloaded after :data:`DAILY_MODEL_IDLE_TTL_S` of idleness.
- ``heavy``: large models loaded on demand for a job and
  unloaded as soon as that job completes; they are far too big
  to leave resident on a personal machine.

The policy decision — which models to load or unload given the
router's reported state, the last-used times and the job
states — is the pure function :func:`plan_tier_actions`, so it
is testable without a router. The manager in
``llama_router.py`` gathers the state, asks the policy, and
performs the calls.

Honesty rule, shared with the rest of the local-model surface:
a model whose load state the router did not report (``loaded``
is None) is never acted on — an unreported state is not a
reported false, and policy must not guess its way into loading
or evicting a model on a guess.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

TIER_RESIDENT = "resident"
TIER_DAILY = "daily"
TIER_HEAVY = "heavy"
MODEL_TIERS = (TIER_RESIDENT, TIER_DAILY, TIER_HEAVY)

# How long the daily model may sit idle before the policy
# unloads it. Ten minutes: long enough that a conversation's
# natural pauses never trigger a reload, short enough that the
# machine's memory is handed back the same evening.
DAILY_MODEL_IDLE_TTL_S = 600.0


@dataclass(frozen=True)
class LlamaPreset:
    """One Aether-owned preset: a named pick, the router model
    id pattern it recognises, its lifecycle tier, a plain
    description for the panel, and the context size the preset
    is meant to run with."""

    id: str
    name: str
    pattern: str
    tier: str
    description: str
    context_size: int


# Order matters: the first preset whose pattern matches a model
# id owns that model, so the embedding preset (whose ids also
# contain size markers) precedes the size-class chat presets.
PRESETS: tuple[LlamaPreset, ...] = (
    LlamaPreset(
        id="nomic-embed",
        name="Nomic Embed",
        pattern="*nomic-embed*",
        tier=TIER_RESIDENT,
        description="Tiny embedding model for memory and workspace "
                    "search. Kept resident so recall never waits on "
                    "a cold load.",
        context_size=8192,
    ),
    LlamaPreset(
        id="llama-3-2-3b",
        name="Llama 3.2 3B",
        pattern="*llama-3.2-3b*",
        tier=TIER_RESIDENT,
        description="Small, fast chat model kept resident for "
                    "instant replies and helper tasks.",
        context_size=8192,
    ),
    LlamaPreset(
        id="qwen-2-5-7b",
        name="Qwen 2.5 7B",
        pattern="*qwen2.5-7b*",
        tier=TIER_DAILY,
        description="The everyday chat model: loaded on demand and "
                    "kept warm for a few minutes between uses.",
        context_size=32768,
    ),
    LlamaPreset(
        id="llama-3-1-8b",
        name="Llama 3.1 8B",
        pattern="*llama-3.1-8b*",
        tier=TIER_DAILY,
        description="Everyday chat model with a long context: "
                    "loaded on demand and kept warm between uses.",
        context_size=32768,
    ),
    LlamaPreset(
        id="qwen-2-5-32b",
        name="Qwen 2.5 32B",
        pattern="*qwen2.5-32b*",
        tier=TIER_HEAVY,
        description="Large model for demanding jobs. Loaded for "
                    "one job and unloaded as soon as it finishes.",
        context_size=32768,
    ),
    LlamaPreset(
        id="llama-3-3-70b",
        name="Llama 3.3 70B",
        pattern="*llama-3.3-70b*",
        tier=TIER_HEAVY,
        description="The biggest local option, for the hardest "
                    "jobs. Loaded per job and unloaded on "
                    "completion; needs a large memory budget.",
        context_size=32768,
    ),
)


def match_preset(model_id: str) -> LlamaPreset | None:
    """The preset owning ``model_id``, or None.

    Matching is a case-insensitive fnmatch over the router's
    model id (an HF-style id such as
    ``ggml-org/gemma-3-4b-it-GGUF:Q4_K_M`` or a bare GGUF file
    name from a models directory). First match wins.
    """
    if not model_id:
        return None
    lowered = model_id.lower()
    for preset in PRESETS:
        if fnmatch.fnmatchcase(lowered, preset.pattern):
            return preset
    return None


def preset_rows(available_model_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The wire rows for the preset catalogue.

    ``available`` is evidence-based: a preset is available only
    when a model id the router actually reported matches its
    pattern. A preset with no matching model on this machine
    reads ``available: False`` — never a hopeful guess.
    """
    ids = [str(m) for m in available_model_ids]
    rows: list[dict[str, Any]] = []
    for preset in PRESETS:
        rows.append({
            "id": preset.id,
            "name": preset.name,
            "tier": preset.tier,
            "description": preset.description,
            "available": any(
                fnmatch.fnmatchcase(m.lower(), preset.pattern) for m in ids),
        })
    return rows


def plan_tier_actions(
    models: list[dict[str, Any]],
    *,
    now: float,
    router_just_started: bool = False,
) -> dict[str, list[str]]:
    """Decide the load/unload actions the tier policy calls for.

    Pure: no I/O, no clock reads — ``now`` is supplied by the
    caller in the same clock as the rows' ``last_used`` values.

    Each row is a dict with:

    - ``id``: the router model id.
    - ``loaded``: True / False / None (None = the router did
      not report the state; such models are never acted on).
    - ``last_used``: seconds (caller's clock) of the model's
      last use, or None when unknown — unknown idleness is not
      idleness, so a daily model with no recorded use is left
      loaded.
    - ``job_state``: ``"active"`` while a job is using the
      model, ``"done"`` once that job completed, else None.

    Rules: resident models load when the router has just
    started and are never auto-unloaded; daily models unload
    after :data:`DAILY_MODEL_IDLE_TTL_S` idle (never mid-job);
    heavy models unload when their job is done. Models no
    preset owns are the user's own business — the policy never
    touches them.
    """
    load: list[str] = []
    unload: list[str] = []
    for row in models:
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        preset = match_preset(model_id)
        if preset is None:
            continue
        loaded = row.get("loaded")
        if loaded is None:
            continue
        if preset.tier == TIER_RESIDENT:
            if router_just_started and loaded is False:
                load.append(model_id)
        elif preset.tier == TIER_DAILY:
            if loaded is True and row.get("job_state") != "active":
                last_used = row.get("last_used")
                if last_used is not None and (
                        now - float(last_used)) > DAILY_MODEL_IDLE_TTL_S:
                    unload.append(model_id)
        elif (preset.tier == TIER_HEAVY and loaded is True
                and row.get("job_state") == "done"):
            unload.append(model_id)
    return {"load": load, "unload": unload}
