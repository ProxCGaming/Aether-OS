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

    P4a: with ``user_config.coder_backend == "miniswe"`` the
    delegation is executed by the mini-SWE backend instead of the
    native loop (see _run_coder_backend). The flag defaults to
    "native", and plan mode always takes the native path (plan mode
    executes nothing), so with the flag off — or in plan mode —
    behaviour is exactly the pre-P4a path below.
    """
    user_config = config.get("configurable", {}).get("user_config")
    if (getattr(user_config, "coder_backend", "native") == "miniswe"
            and not state.get("plan_mode")):
        return await _run_coder_backend(state, config)
    # save_artifact is passed from this module's globals at call time so
    # the module-level patch seam (tests stub nodes.coder.save_artifact)
    # keeps working now that the shared body lives in runner.py.
    return await run_specialist(
        CODER_SPEC, state, config, save_artifact=save_artifact)


# ---------------------------------------------------------------------------
# P4a: the mini-SWE backend path
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


async def _run_coder_backend(
        state: AetherState, config: RunnableConfig) -> dict:
    """Execute one coder delegation on the mini-SWE backend and
    shape the result like the native runner's final-answer return,
    so the supervisor and executor see the same surface either way.
    The backend's diff is PROPOSED, never applied: it rides the
    assistant message (and the coder artifact) for the existing
    approval flow to present."""
    provider = _backend_provider(config)
    if provider is None:
        result = CoderTaskResult(
            status="failed", diff="",
            summary=("Coder backend 'miniswe' is selected but no "
                     "provider is configured for this run."),
            files_touched=[], cost_actual=0.0, transcript_ref="",
            backend="miniswe")
    else:
        backend, model_alias = _build_miniswe_backend(config, provider)
        request = CoderTaskRequest(
            task_briefing=_backend_task_briefing(state),
            repo_path=_backend_repo_path(config),
            model_alias=model_alias)
        result = await asyncio.to_thread(backend.run, request)

    content = result.summary
    if result.status != STATUS_DONE:
        content = f"[coder backend: miniswe — {result.status}] {content}"
    if result.diff:
        content += (
            "\n\nProposed diff (produced in a throwaway worktree; "
            "NOT applied to the working tree):\n\n```diff\n"
            f"{result.diff}\n```")
    entry = {
        "agent": "coder",
        "backend": result.backend or "miniswe",
        "timestamp": time.time(),
        "status": result.status,
        "cost_actual": result.cost_actual,
        "tokens_actual": result.tokens_actual,
        "files_touched": list(result.files_touched),
        "transcript_ref": result.transcript_ref,
    }
    out: dict = {
        "current_phase": CODER_SPEC.phase,
        "messages": [{"role": "assistant", "content": content}],
        "delegation_log": [entry],
    }
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(
        task_id, CODER_SPEC.artifact_kind, CODER_SPEC.artifact_filename,
        content)
    out["artifact_paths"] = {CODER_SPEC.artifact_key: artifact_path}
    return out
