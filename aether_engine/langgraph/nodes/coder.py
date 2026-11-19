import asyncio
import logging
import os
import time

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact
from aether_engine.langgraph.backends.coder_backend import (
    STATUS_DONE,
    CoderTaskRequest,
    CoderTaskResult,
)
from aether_engine.langgraph.nodes.runner import SpecialistSpec, run_specialist
from aether_engine.langgraph.state import (
    AetherState,
    blackboard_briefing,
    message_dicts,
)

logger = logging.getLogger("aether_engine.langgraph.coder")

CODER_SPEC = SpecialistSpec(
    name="coder",
    display_name="Coder",
    role_prompt="Write, test, and debug code based on the current plan and context.",
    fallback_prompt="Please proceed with writing, testing, or debugging the code based on the current state.",
    proceed_prompt="Please proceed with writing, testing, or debugging the code based on the current state.",
    phase="coding",
    artifact_kind="code",
    artifact_filename="coder_output.txt",
    artifact_key="coder_output",
    tail_extras=None,
)


async def coder_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Coder node writes, tests, and debugs code.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).

    P4a/P4b: with ``user_config.coder_backend`` set to "miniswe"
    or "openhands" the delegation is executed by that backend
    instead of the native loop (see _run_coder_backend). The flag
    defaults to "native", and plan mode always takes the native
    path (plan mode executes nothing), so with the flag off — or
    in plan mode — behaviour is exactly the pre-P4a path below.
    The openhands backend reports itself unavailable-with-reason
    until its SDK and Docker are present; that surfaces here as an
    honest failed delegation, never a fake run.

    P2a: with ``user_config.coder_cascade`` also on (and routing
    in "auto", and no coder pin), that backend delegation runs as
    the verifier cascade instead of a single backend run — see
    _cascade_ladder. The cascade flag defaults off, so this
    changes nothing until the owner enables it.
    """
    user_config = config.get("configurable", {}).get("user_config")
    selected = getattr(user_config, "coder_backend", "native")
    if selected in ("miniswe", "openhands") and not state.get("plan_mode"):
        builder = (_build_miniswe_backend if selected == "miniswe"
                   else _build_openhands_backend)
        return await _run_coder_backend(
            state, config, builder=builder, backend_name=selected)
    # save_artifact is passed from this module's globals at call time so
    # the module-level patch seam (tests stub nodes.coder.save_artifact)
    # keeps working now that the shared body lives in runner.py.
    return await run_specialist(
        CODER_SPEC, state, config, save_artifact=save_artifact)


# ---------------------------------------------------------------------------
# P4a/P4b: the external backend path (mini-SWE cheap tier, OpenHands
# strong tier — one shared result-shaping path for both)
# ---------------------------------------------------------------------------


def _backend_provider(config: RunnableConfig):
    """The provider whose LiteLLM route the backend child will use.

    A coder pin in ``agent_models`` constructs its provider exactly
    the way the native runner does (same factory, same run
    constraints); an unusable pin falls back to the run provider,
    again like the runner. ``None`` only when the run itself has no
    provider.
    """
    configurable = config.get("configurable", {})
    provider = configurable.get("provider")
    pin = (configurable.get("agent_models") or {}).get("coder")
    secret_store = configurable.get("secret_store")
    if pin and pin != "inherit" and ":" in pin and secret_store:
        p_name, m_name = pin.split(":", 1)
        try:
            from aether_engine.orchestration.core import (
                _create_provider_instance,
            )

            return _create_provider_instance(
                p_name, m_name, secret_store,
                local_only=bool(configurable.get("local_only")),
                require_vision=bool(configurable.get("require_vision")),
                user_config=configurable.get("user_config"),
            )
        except Exception as exc:  # noqa: BLE001 — mirrors the runner's pin fallback.
            logger.warning(
                "Coder agent_models pin %r unusable for the backend, "
                "using the run provider: %s", pin, exc)
    return provider


def _build_miniswe_backend(config: RunnableConfig, provider):
    """Construct the backend + the LiteLLM model alias it will run.

    The route handed to the wrapper is the provider's own: its
    LiteLLM model string, its API key (injected into the child env
    by the wrapper — never into the request file), and its base URL
    when it has one. Module-level so tests can substitute a stub
    backend without a subprocess.
    """
    from aether_engine.langgraph.backends.coder_miniswe import (
        MiniSweCoderBackend,
        route_env_for_model,
    )
    from aether_engine.providers.litellm_provider import (
        resolve_litellm_model,
    )

    model_alias = getattr(provider, "litellm_model", None) or resolve_litellm_model(
        str(getattr(provider, "model", "") or ""),
        str(getattr(provider, "provider_name", "") or ""))
    route_env = route_env_for_model(
        model_alias, getattr(provider, "api_key", None))
    base_url = getattr(provider, "base_url", None)
    model_kwargs = {"api_base": base_url} if base_url else {}
    backend = MiniSweCoderBackend(
        route_env=route_env, model_kwargs=model_kwargs)
    return backend, model_alias


def _build_openhands_backend(config: RunnableConfig, provider):
    """Construct the OpenHands backend + the LiteLLM model alias it
    will run. Same route discipline as the mini-SWE builder: the
    provider's own LiteLLM model string, its API key handed to the
    wrapper (which injects it into the child env — never into the
    request file), and its base URL when it has one. Module-level
    so tests can substitute a stub backend without a subprocess.
    """
    from aether_engine.langgraph.backends.coder_miniswe import (
        route_env_for_model,
    )
    from aether_engine.langgraph.backends.coder_openhands import (
        OpenHandsCoderBackend,
    )
    from aether_engine.providers.litellm_provider import (
        resolve_litellm_model,
    )

    model_alias = getattr(provider, "litellm_model", None) or resolve_litellm_model(
        str(getattr(provider, "model", "") or ""),
        str(getattr(provider, "provider_name", "") or ""))
    route_env = route_env_for_model(
        model_alias, getattr(provider, "api_key", None))
    base_url = getattr(provider, "base_url", None)
    model_kwargs = {"api_base": base_url} if base_url else {}
    backend = OpenHandsCoderBackend(
        route_env=route_env, model_kwargs=model_kwargs)
    return backend, model_alias


def _backend_repo_path(config: RunnableConfig) -> str:
    configurable = config.get("configurable", {})
    roots = configurable.get("workspace_roots")
    if not roots:
        user_config = configurable.get("user_config")
        roots = getattr(user_config, "workspace_roots", None)
    if roots:
        return str(roots[0])
    return os.getcwd()


def _backend_task_briefing(state: AetherState) -> str:
    """The delegation as a standalone task text for the backend:
    the latest user instruction (or the original prompt), plus the
    same plan / memory-briefing / blackboard context the native
    runner folds into its system prompt."""
    task = ""
    for msg in reversed(message_dicts(state.get("messages", []))):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            task = msg["content"].strip()
            break
    if not task:
        task = (state.get("original_prompt") or "").strip() \
            or CODER_SPEC.fallback_prompt
    parts = [task]
    plan_text = (state.get("plan") or "").strip()
    if plan_text and plan_text != "No plan currently.":
        parts.append(f"## Current plan & context\n{plan_text}")
    briefing = (state.get("context_briefing") or "").strip()
    if briefing:
        parts.append(f"## Memory briefing\n{briefing}")
    board = blackboard_briefing(state)
    if board:
        parts.append(f"## Shared team blackboard\n{board}")
    return "\n\n".join(parts)


def _backend_content(result: CoderTaskResult) -> str:
    """The assistant-message text for one backend result: the
    summary (status-prefixed when the run did not complete) plus
    the proposed diff, which is never applied by the backend."""
    content = result.summary
    if result.status != STATUS_DONE:
        content = (f"[coder backend: {result.backend or 'miniswe'}"
                   f" — {result.status}] {content}")
    if result.diff:
        content += (
            "\n\nProposed diff (produced in a throwaway worktree; "
            "NOT applied to the working tree):\n\n```diff\n"
            f"{result.diff}\n```")
    return content


def _backend_out(state: AetherState, content: str, entries: list) -> dict:
    """The node return for a backend delegation: shaped like the
    native runner's final-answer return, so the supervisor and
    executor see the same surface either way."""
    out: dict = {
        "current_phase": CODER_SPEC.phase,
        "messages": [{"role": "assistant", "content": content}],
        "delegation_log": entries,
    }
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(
        task_id, CODER_SPEC.artifact_kind, CODER_SPEC.artifact_filename,
        content)
    out["artifact_paths"] = {CODER_SPEC.artifact_key: artifact_path}
    return out


async def _run_coder_backend(
        state: AetherState, config: RunnableConfig, *,
        builder, backend_name: str) -> dict:
    """Execute one coder delegation on an external backend and
    shape the result like the native runner's final-answer return,
    so the supervisor and executor see the same surface either way.
    The backend's diff is PROPOSED, never applied: it rides the
    assistant message (and the coder artifact) for the existing
    approval flow to present. With the P2a cascade enabled (and
    the mini-SWE backend selected), the delegation instead runs
    as a verifier cascade across backend rungs — see
    _cascade_ladder."""
    provider = _backend_provider(config)
    if provider is None:
        result = CoderTaskResult(
            status="failed", diff="",
            summary=(f"Coder backend '{backend_name}' is selected but no "
                     "provider is configured for this run."),
            files_touched=[], cost_actual=0.0, transcript_ref="",
            backend=backend_name)
    else:
        if backend_name == "miniswe":
            ladder = _cascade_ladder(state, config, provider)
            if ladder is not None:
                return await _run_coder_cascade(state, config, ladder)
        backend, model_alias = builder(config, provider)
        request = CoderTaskRequest(
            task_briefing=_backend_task_briefing(state),
            repo_path=_backend_repo_path(config),
            model_alias=model_alias)
        result = await asyncio.to_thread(backend.run, request)

    entry = {
        "agent": "coder",
        "backend": result.backend or backend_name,
        "timestamp": time.time(),
        "status": result.status,
        "cost_actual": result.cost_actual,
        "tokens_actual": result.tokens_actual,
        "files_touched": list(result.files_touched),
        "transcript_ref": result.transcript_ref,
    }
    return _backend_out(state, _backend_content(result), [entry])


# ---------------------------------------------------------------------------
# P2a: the verifier cascade over the backend ladder
# ---------------------------------------------------------------------------


def _cascade_ladder(
        state: AetherState, config: RunnableConfig, decided_provider):
    """Build the cascade's rungs (cheap → decided → above), or
    None when the cascade does not apply to this delegation.

    The cascade applies only when ALL of the plan's gates hold:
    the owner enabled it (``coder_cascade``), routing is "auto",
    and the coder is not pinned (a pin is never cascaded, in any
    mode). The decided rung is S14's own decision for the
    delegation — the same route() the native runner serves — and
    the cheap rung is the best candidate one tier below it; when
    the decided tier is already the pool's lowest, or any rung's
    provider cannot be constructed, there is no cascade and the
    caller serves the decided provider exactly as before. Any
    failure in the routing machinery degrades the same way —
    routing unavailability is never a run failure.
    """
    configurable = config.get("configurable", {})
    user_config = configurable.get("user_config")
    if not getattr(user_config, "coder_cascade", False):
        return None
    pin = (configurable.get("agent_models") or {}).get("coder")
    if pin and pin != "inherit":
        return None
    from aether_engine.langgraph import model_routing

    if model_routing.routing_mode(user_config) != "auto":
        return None
    incumbent_provider = getattr(decided_provider, "provider_name", None)
    incumbent_model = getattr(decided_provider, "model", None)
    if not incumbent_provider or not incumbent_model:
        return None
    try:
        from aether_engine.langgraph.coder_cascade import CascadeRung
        from aether_engine.state import engine_state

        candidates = model_routing.collect_candidates(
            registry=engine_state.model_registry,
            configured_providers=engine_state.configured_providers,
            health_manager=engine_state.health_manager,
            incumbent_provider=str(incumbent_provider),
            incumbent_model=str(incumbent_model),
            local_only=bool(configurable.get("local_only")))
        if not candidates:
            return None
        signals = model_routing.signals_for_state("coder", state)
        history = model_routing.collect_history(candidates)
        choice = model_routing.route(signals, candidates, history)
        if choice is None:
            return None

        def _provider_for(a_choice):
            if (a_choice.provider, a_choice.model) == (
                    str(incumbent_provider), str(incumbent_model)):
                return decided_provider
            return model_routing.construct_choice_provider(a_choice, config)

        decided = _provider_for(choice)
        if decided is None:
            return None
        # The cheap rung: the best-ranked candidate strictly below
        # the decided tier (route()'s own ranking, so the cascade
        # demotes to the model S14 itself would trust most there).
        group, cheap_tier = model_routing._tier_group(
            candidates, choice.tier - 1, None)
        if cheap_tier >= choice.tier or not group:
            return None  # decided tier is already the floor: no cascade
        cheap_choice = min(
            group, key=lambda c: model_routing._rank_key(c, "coder", history))
        if (cheap_choice.provider, cheap_choice.model) == (
                choice.provider, choice.model):
            return None
        cheap = _provider_for(model_routing.RoutingChoice(
            provider=cheap_choice.provider, model=cheap_choice.model,
            tier=cheap_tier, source="auto",
            reason="P2a cascade: the cheap first rung."))
        if cheap is None:
            return None
        rung_providers = [(cheap_tier, cheap), (choice.tier, decided)]
        # The above-decided rung exists only on the owner's
        # two-step ladder (Gate 3); run_cascade caps attempts at
        # MAX_ATTEMPTS either way.
        if getattr(user_config, "coder_cascade_two_step", False):
            above = model_routing.escalate(
                signals, candidates, history,
                current_provider=choice.provider,
                current_model=choice.model,
                current_tier=choice.tier)
            if above is not None:
                above_provider = _provider_for(above)
                if above_provider is not None:
                    rung_providers.append((above.tier, above_provider))
        rungs = []
        for tier, rung_provider in rung_providers:
            backend, model_alias = _build_miniswe_backend(
                config, rung_provider)
            rungs.append(CascadeRung(
                tier=tier,
                tier_name=model_routing.TIER_NAMES.get(tier, str(tier)),
                model_alias=model_alias,
                provider_name=str(
                    getattr(rung_provider, "provider_name", "") or ""),
                model=str(getattr(rung_provider, "model", "") or ""),
                backend_name=getattr(backend, "name", "miniswe"),
                run=backend.run))
        return rungs if len(rungs) >= 2 else None
    except Exception:
        logger.warning(
            "Coder cascade ladder unavailable; serving the decided "
            "tier directly", exc_info=True)
        return None


async def _run_coder_cascade(
        state: AetherState, config: RunnableConfig, ladder) -> dict:
    """Run one coder delegation as a P2a verifier cascade and
    shape the outcome: the final attempt's result (its diff rides
    the message as always), the one-line cascade trail, one
    delegation-log entry per attempt, a ``model_cascade`` event
    per attempt, and an S14 outcome row per attempt. A ladder
    that ends in rejection surfaces the rejection in the content
    — the result stands, but never silently."""
    from aether_engine.langgraph import coder_cascade, model_routing

    user_config = config.get("configurable", {}).get("user_config")
    outcome = await asyncio.to_thread(
        coder_cascade.run_cascade,
        rungs=ladder,
        task_briefing=_backend_task_briefing(state),
        repo_path=_backend_repo_path(config),
        two_step=bool(getattr(user_config, "coder_cascade_two_step", False)))
    result = outcome.result
    for attempt in outcome.attempts:
        coder_cascade.publish_cascade_activity(state, attempt)
        model_routing.record_model_outcome(
            attempt.get("provider_name"), attempt.get("model_name"),
            "coder",
            "success" if attempt["verdict"] == "accepted" else "failure")
    content = _backend_content(result)
    content += f"\n\n[coder cascade] {outcome.summary_line}"
    if outcome.final_rejection_reason:
        content += (
            "\n\n[coder cascade] The final attempt was rejected by "
            f"verification ({outcome.final_rejection_reason}); its "
            "result stands as returned above.")
    entries = [{
        "agent": "coder",
        "backend": attempt["backend"],
        "timestamp": attempt["timestamp"],
        "status": attempt["status"],
        "cost_actual": attempt["cost_actual"],
        "tokens_actual": attempt["tokens_actual"],
        "files_touched": attempt["files_touched"],
        "transcript_ref": attempt["transcript_ref"],
        "cascade_attempt": attempt["attempt"],
        "cascade_tier": attempt["tier_name"],
        "cascade_model": attempt["model"],
        "cascade_verdict": attempt["verdict"],
        "cascade_verifier": attempt["verifier"],
        "cascade_reason": attempt["reason"],
    } for attempt in outcome.attempts]
    return _backend_out(state, content, entries)
