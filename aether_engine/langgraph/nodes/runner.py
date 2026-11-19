"""Shared runner for the specialist nodes (coder / planner / researcher).

The three specialist nodes used to carry three hand-copied versions of
the same ~190-line body; every shared fix (the E1-13 prewalk gate, the
M17/E5-F02 provider-construction constraints, the S1-M5 queued-call
drain) had to be propagated three times. This module is the single
body, parameterised by a frozen :class:`SpecialistSpec`; each node
module is now a declarative spec plus a thin wrapper keeping its exact
public name (``coder_node`` / ``planner_node`` / ``researcher_node``).

``run_specialist`` takes the artifact saver as an optional keyword:
the wrappers pass their own module-global ``save_artifact`` so the
long-standing patch seam (tests monkeypatch
``aether_engine.langgraph.nodes.<agent>.save_artifact``) keeps working
exactly as it did when the body lived in each node module.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact as _default_save_artifact
from aether_engine.langgraph.advisor import advisor_notes
from aether_engine.langgraph.compaction import (
    context_summary_section,
    model_facing_messages,
)
from aether_engine.langgraph.nodes.tool_calls import (
    assistant_tool_calls,
    drain_queued_tool_call,
    pending_and_queued,
)
from aether_engine.langgraph.state import (
    AetherState,
    blackboard_briefing,
    message_dicts,
)
from aether_engine.langgraph.structured import apply_output_schema
from aether_engine.langgraph.thinking import forward_thinking
from aether_engine.providers.base import ProviderError


def _tool_offer_entry(agent: str, offered: list, called: list) -> dict:
    """P2c step 2: one offered-vs-called record for a specialist model
    round, appended to ``delegation_log`` beside the supervisor's
    decision entries.

    The entry carries no ``decision`` key, and every existing reader
    of the log keys on ``decision.next`` and skips it
    (``delegation_briefing``, the executor's delegation event, the
    Task Ledger, S14's briefing lookup). Its consumers are P2a's
    cascade and P3a's learned router: which offered tools a
    delegation never calls, and whether a call ever lands outside
    the offer.
    """
    return {
        "agent": agent,
        "timestamp": time.time(),
        "tools_offered": list(offered),
        "tools_called": list(called),
    }


@dataclass(frozen=True)
class SpecialistSpec:
    """Everything that genuinely differs between the specialist nodes."""

    name: str  # config/state key: "coder" | "planner" | "researcher"
    display_name: str  # capitalised form used in prompts and log lines
    role_prompt: str  # the "You are a … agent." sentence's verb phrase
    fallback_prompt: str  # seeded as the user turn when state has none
    proceed_prompt: str  # appended when the last message is not user/tool
    phase: str  # value written to state["current_phase"]
    artifact_kind: str  # save_artifact kind
    artifact_filename: str  # save_artifact filename
    artifact_key: str  # key under result["artifact_paths"]
    tail_extras: Callable[[str], dict] | None  # extra result keys from content


async def run_specialist(
    spec: SpecialistSpec,
    state: AetherState,
    config: RunnableConfig,
    *,
    save_artifact: Callable[..., str] | None = None,
) -> dict:
    """Run one specialist turn for ``spec`` (see module docstring)."""
    if save_artifact is None:
        save_artifact = _default_save_artifact
    # Logger identity is per-specialist (aether_engine.langgraph.<name>),
    # matching the loggers the individual node modules always used.
    logger = logging.getLogger(f"aether_engine.langgraph.{spec.name}")

    provider = config.get("configurable", {}).get("provider")

    agent_models = config.get("configurable", {}).get("agent_models", {})
    agent_tools = config.get("configurable", {}).get("agent_tools", {})
    secret_store = config.get("configurable", {}).get("secret_store")

    agent_model_override = agent_models.get(spec.name)
    # 15 Nov 2026: a pin naming a KNOWN_UNUSABLE_MODELS entry (e.g. a
    # stale gemini-2.5-flash pin) is refused here and the agent runs
    # on the run's default provider — the model selected in Settings.
    # Without this, the pin constructs fine and the provider call
    # 404s mid-run, failing the whole task.
    from aether_engine.langgraph.model_routing import (
        note_pin_refused,
        pin_targets_unusable_model,
    )

    if pin_targets_unusable_model(agent_model_override):
        note_pin_refused(spec.name, agent_model_override)
        agent_model_override = None
    # S14: a pin is any non-"inherit" value, captured BEFORE the
    # prewalk split below can null the override for this round —
    # a pinned agent is never auto-routed and never escalated, in
    # either routing mode (langgraph/model_routing.py).
    _pin_present = bool(agent_model_override) and agent_model_override != "inherit"
    # E1-13 prewalk: for prewalk-enabled agents the per-agent model is
    # the EXECUTION model — until the first write/execute tool lands,
    # the agent plans on the run's (strong) provider instead.
    _uc = config.get("configurable", {}).get("user_config")
    if agent_model_override and spec.name in (getattr(_uc, "prewalk_agents", None) or []) and not state.get("write_tool_used"):
        agent_model_override = None
    if agent_model_override and agent_model_override != "inherit" and secret_store:
        try:
            # Format is "provider_name:model_name"
            parts = agent_model_override.split(":", 1)
            if len(parts) == 2:
                p_name, m_name = parts
                from aether_engine.orchestration.core import _create_provider_instance
                # M17 residual: the run's routing constraints (set by
                # the executor from the routing decision) bind this
                # construction too — no silent cloud fallback for a
                # local-only task's images.
                _cfg = config.get("configurable", {})
                provider = _create_provider_instance(
                    p_name, m_name, secret_store,
                    local_only=bool(_cfg.get("local_only")),
                    require_vision=bool(_cfg.get("require_vision")),
                    # E5-F02: the run's effective config governs this
                    # construction's fallback policy too.
                    user_config=_uc,
                )
        except Exception as e:  # noqa: BLE001 — an unusable per-agent model override falls back to the graph provider.
            logger.warning(f"{spec.display_name} agent_models override {agent_model_override!r} unusable, using default provider: {e}")

    if not provider or not hasattr(provider, "call_stream"):
        raise RuntimeError("Valid provider not found in graph config")

    # S1-M5: if a previous response queued parallel tool calls, drain
    # the head into pending_tool_call instead of calling the model —
    # the assistant message with all the calls is already recorded.
    drained = drain_queued_tool_call(state)
    if drained is not None:
        logger.info(
            "%s draining queued tool call %s (%s); %d still queued",
            spec.display_name,
            drained["pending_tool_call"].get("call_id"),
            drained["pending_tool_call"].get("name"),
            len(drained["queued_tool_calls"]),
        )
        return drained

    # S14: automatic model routing — what "inherit" resolves to
    # for THIS invocation, decided per delegation from the task's
    # signals and the recorded history (langgraph/model_routing).
    # Consulted only for unpinned agents in "auto" mode; "off"
    # (the default) and pins take the exact pre-S14 path above, so
    # with routing off this block is inert and the provider — and
    # every prompt built below — is unchanged. An S13 child's
    # retry carries the seam's step-up flag in its config; the
    # resolver honours it there.
    routing_ctx = None
    escalated = False
    if not _pin_present:
        from aether_engine.langgraph import model_routing

        if model_routing.routing_mode(_uc) == "auto":
            provider, routing_ctx = model_routing.resolve_for_specialist(
                agent=spec.name, state=state, config=config,
                run_provider=provider)
            if routing_ctx is not None and routing_ctx.choice.source == "escalation":
                escalated = True

    registry = config.get("configurable", {}).get("tool_registry")
    all_tools = registry.get_definitions() if registry else []

    # P2c Filter 1: the per-agent tool ceiling (tools/ceilings.py)
    # supersedes the legacy `agent_tools` allow-list at this
    # resolution point. A legacy list still applies when configured,
    # but only as an intersection — it narrows the ceiling, never
    # widens it. A tool already used in this conversation is kept
    # with a DO NOT USE description instead of being removed (the
    # legacy behaviour), so in-flight calls never dangle. The
    # plan-mode and desktop-actuation filters below stay separate,
    # later concerns — they are not ceilings and are not merged in.
    from aether_engine.tools.ceilings import apply_tool_ceiling

    used_tools = set()
    for msg in message_dicts(state.get("messages", [])):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls"):
                if isinstance(tc, dict):
                    used_tools.add(tc.get("function", {}).get("name"))
    _legacy_allowed = agent_tools.get(spec.name)
    if registry is not None and hasattr(registry, "get_tools"):
        risk_by_name = {t.name: t.risk for t in registry.get_tools()}
    elif registry is not None:
        # Narrow registry doubles (older test stubs) may expose only
        # the pre-P2c surface — definitions + read_only_names. Judge
        # everything not known read-only as EXECUTE (fail closed,
        # the registry's own default for an unlabelled tool).
        from aether_engine.tools.registry import ToolRisk as _ToolRisk

        _read_only = (registry.read_only_names()
                      if hasattr(registry, "read_only_names") else set())
        risk_by_name = {}
        for _definition in all_tools:
            _name = _definition.get("function", {}).get("name")
            if _name:
                risk_by_name[_name] = (_ToolRisk.READ_ONLY
                                       if _name in _read_only
                                       else _ToolRisk.EXECUTE)
    else:
        risk_by_name = {}
    tools = apply_tool_ceiling(
        spec.name,
        all_tools,
        risk_by_name,
        legacy_allowed=_legacy_allowed if isinstance(_legacy_allowed, list) else None,
        used_names=used_tools,
    )

    # Plan mode (Epic 1, E1-09): only READ_ONLY tools are even offered.
    if state.get("plan_mode") and registry:
        keep = registry.read_only_names()
        tools = [t for t in tools if t.get("function", {}).get("name") in keep]

    # Master build S20: desktop actuation is offered ONLY when
    # Computer Use control is fully permitted (master + Control +
    # the one-time acknowledgement — desktop.control_permitted).
    # The filter runs on the final list (after the allow-list and
    # plan-mode passes; plan mode already drops it as a WRITE
    # tool) and removes the tool outright rather than disabling
    # its description, so with control off — the default — the
    # offered tools, and therefore the prompt, are byte-identical
    # to pre-S20.
    from aether_engine.desktop import control_permitted
    from aether_engine.desktop.policy import DESKTOP_TOOL_NAMES

    if not control_permitted(_uc):
        tools = [t for t in tools
                 if t.get("function", {}).get("name") not in DESKTOP_TOOL_NAMES]

    # S13: bounded sub-agent spawning — a specialist may spawn a
    # bounded sub-agent for a focused sub-task (langgraph/subagents).
    # Offered only; every bound is enforced at execution time in
    # subagents.execute_spawn. Never offered inside a sub-agent run:
    # depth (supervisor -> specialist -> sub-agent) is enforced by
    # construction, the child's surface is built without this tool.
    # Never offered in plan mode either: spawning is an execution
    # act, and plan mode's contract is plan-only (the supervisor
    # spawn policy takes the same stance). The system prompt is
    # unchanged either way — the contract rides the tool
    # description — so with sub-agents disabled the prompt is
    # byte-identical to pre-S13.
    if (not state.get("plan_mode")
            and not config.get("configurable", {}).get("is_subagent_run")):
        from aether_engine.langgraph.subagents import (
            spawn_tool_definition,
            subagent_settings,
        )

        if subagent_settings(_uc).enabled:
            tools = [*tools, spawn_tool_definition()]

    # P2c step 2: the offered set for this model round — every filter
    # above has run, so this is exactly what the model is about to be
    # offered. Recorded with the round's calls at the returns below
    # (see _tool_offer_entry).
    offered_names = [t.get("function", {}).get("name") for t in tools]

    plan_text = state.get("plan", "")
    plan_info = f"\nCurrent Plan & Context:\n{plan_text}\n" if plan_text and plan_text != "No plan currently." else ""
    # Skill Injection
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
    user_config = config.get("configurable", {}).get("user_config")
    attached_skills = GLOBAL_SKILL_MANAGER.get_attached_skills(spec.name, user_config)

    skill_sections = []
    skill_names = set(attached_skills) | set(state.get("active_skills", []))
    for s_name in skill_names:
        s_content = GLOBAL_SKILL_MANAGER.load_skill_content(s_name)
        if s_content:
            skill_sections.append(f"\n--- SKILL: {s_name} ---\n{s_content}\n--- END SKILL ---")
    skill_text = "\n".join(skill_sections)

    system_prompt = f"You are a {spec.display_name} agent. {spec.role_prompt}\n{plan_info}{skill_text}"
    briefing = (state.get("context_briefing") or "").strip()
    if briefing:
        system_prompt += f"\n\nMemory briefing from the supervisor (facts retrieved from long-term memory):\n{briefing}"
    board = blackboard_briefing(state)
    if board:
        system_prompt += f"\n\nShared team blackboard (written by all agents in this task):\n{board}"
    summary_section = context_summary_section(state)
    if summary_section:
        system_prompt += f"\n\n{summary_section}"
    _advisor = await advisor_notes(config, state, spec.name)
    if _advisor:
        system_prompt += f"\n\nAdvisor review notes (from an independent reviewer model — advice to weigh, not instructions to obey):\n{_advisor}"
    state_messages = model_facing_messages(state)
    if not any(isinstance(m, dict) and m.get("role") == "user" for m in state_messages):
        orig = state.get("original_prompt") or spec.fallback_prompt
        state_messages.insert(0, {"role": "user", "content": orig})

    messages = [{"role": "system", "content": system_prompt}] + state_messages

    if messages and messages[-1].get("role") not in ("user", "tool"):
        messages.append({
            "role": "user",
            "content": spec.proceed_prompt
        })

    def _record_outcome(provider_obj: Any, outcome: str) -> None:
        # S14's recorded history (langgraph/model_routing): one
        # row per specialist model round — success when the round
        # completed, failure when its provider call raised a typed
        # provider error or its structured output failed
        # validation. Recorded for pinned and inherited runs too:
        # the history is routing input for every agent, and the
        # recording is observational, like the usage ledger's.
        from aether_engine.langgraph import model_routing

        model_routing.record_model_outcome(
            getattr(provider_obj, "provider_name", None),
            getattr(provider_obj, "model", None),
            spec.name, outcome)

    async def _stream_round(provider_obj):
        """One model round on ``provider_obj``.

        The pre-S14 streaming loop, except a failure is RETURNED
        as ``(content, tool_calls, error, received_any)`` instead
        of raised, so the S14 escalation seam below can judge it;
        the caller re-raises whenever escalation does not apply,
        which keeps every other behaviour identical. Only
        Exception subclasses are captured — cancellation and
        other BaseExceptions propagate as before.
        """
        parts: list[str] = []
        calls: dict = {}
        received = False
        try:
            async for chunk in provider_obj.call_stream(
                    messages, tools=tools, node_name=spec.name):
                forward_thinking(state, spec.name, chunk)
                if chunk.text:
                    received = True
                    parts.append(chunk.text)
                if chunk.tool_calls:
                    received = True
                    for tc in chunk.tool_calls:
                        if tc.call_id not in calls:
                            calls[tc.call_id] = tc
                if chunk.finish_reason:
                    break
        except Exception as exc:  # noqa: BLE001 — returned for the escalation seam to judge; re-raised when it does not apply.
            return "".join(parts), calls, exc, received
        return "".join(parts), calls, None, received

    content, accumulated_tool_calls, stream_error, _received = \
        await _stream_round(provider)
    if stream_error is not None:
        if isinstance(stream_error, ProviderError):
            _record_outcome(provider, "failure")
        # S14 escalation: ONE step up to the next tier, only for
        # an auto-routed (unpinned) invocation, only for a typed
        # provider failure that arrived before ANY output streamed
        # — a mid-stream failure has already shown partial work,
        # and restarting it on another model would corrupt the
        # answer; policy/approval denials never reach this seam
        # (they are judged in the tool pipeline, not the model
        # call), and non-provider errors are bugs, not model
        # outcomes. Everything else re-raises untouched.
        escalated_provider = None
        if (routing_ctx is not None and not _received
                and isinstance(stream_error, ProviderError)):
            from aether_engine.langgraph import model_routing

            esc_choice = model_routing.escalate(
                routing_ctx.signals, routing_ctx.candidates,
                routing_ctx.history,
                current_provider=str(getattr(provider, "provider_name", "")),
                current_model=str(getattr(provider, "model", "")),
                current_tier=routing_ctx.choice.tier)
            if esc_choice is not None:
                escalated_provider = model_routing.construct_choice_provider(
                    esc_choice, config)
                if escalated_provider is not None:
                    routing_ctx.choice = esc_choice
                    escalated = True
                    model_routing.publish_routing_activity(
                        state, spec.name, esc_choice)
                    logger.warning(
                        "%s model %s failed (%s); escalating once to %s",
                        spec.display_name,
                        getattr(provider, "model", "?"), stream_error,
                        esc_choice.model)
        if escalated_provider is None:
            raise stream_error
        provider = escalated_provider
        content, accumulated_tool_calls, stream_error, _received = \
            await _stream_round(provider)
        if stream_error is not None:
            if isinstance(stream_error, ProviderError):
                _record_outcome(provider, "failure")
            raise stream_error

    if accumulated_tool_calls:
        _record_outcome(provider, "success")
        # S1-M5: the first call is pending for this tool-loop round;
        # any parallel calls are queued in state (checkpointed, so
        # they survive the approval pause) and drained one per round
        # instead of being silently discarded.
        pending, queued = pending_and_queued(accumulated_tool_calls)

        assistant_msg = {
            "role": "assistant",
            "content": content or "",
            "tool_calls": assistant_tool_calls(accumulated_tool_calls),
        }
        if queued:
            logger.info(
                "%s queued %d parallel tool call(s) beyond the first: %s",
                spec.display_name,
                len(queued),
                [q["name"] for q in queued],
            )

        return {
            "messages": [assistant_msg],
            "pending_tool_call": pending,
            "queued_tool_calls": queued,
            "delegation_log": [_tool_offer_entry(
                spec.name, offered_names,
                [tc["function"]["name"]
                 for tc in assistant_tool_calls(accumulated_tool_calls)])],
        }

    # E1-10: validate against the specialist's output schema, if configured.
    # S14: a validation failure past the same-model correction is
    # the second escalation seam (audit G5 named this retry point):
    # in auto mode the correction ladder gets ONE attempt on the
    # next tier up, inside apply_output_schema. Never when the
    # invocation already escalated, and never for a pinned agent
    # (routing_ctx is None then, as it is whenever routing is off).
    _esc_choice = None
    if routing_ctx is not None and not escalated:
        from aether_engine.langgraph import model_routing

        _esc_choice = model_routing.escalate(
            routing_ctx.signals, routing_ctx.candidates,
            routing_ctx.history,
            current_provider=str(getattr(provider, "provider_name", "")),
            current_model=str(getattr(provider, "model", "")),
            current_tier=routing_ctx.choice.tier)
    # The stronger model is constructed LAZILY, only if the
    # same-model correction actually fails (apply_output_schema
    # accepts a zero-arg factory): a run whose output validates
    # first time never builds a provider it will not call.
    _esc_holder: dict[str, Any] = {}

    def _esc_factory():
        if _esc_choice is None:
            return None
        from aether_engine.langgraph import model_routing

        obj = model_routing.construct_choice_provider(_esc_choice, config)
        if obj is not None:
            _esc_holder["provider"] = obj
        return obj

    _esc_fired: list[bool] = []

    def _on_escalation() -> None:
        _esc_fired.append(True)
        if routing_ctx is not None and _esc_choice is not None:
            routing_ctx.choice = _esc_choice
            from aether_engine.langgraph import model_routing

            model_routing.publish_routing_activity(
                state, spec.name, _esc_choice)
            logger.warning(
                "%s structured output failed validation on %s; "
                "escalating the correction once to %s",
                spec.display_name, getattr(provider, "model", "?"),
                _esc_choice.model)

    content, structured_entry = await apply_output_schema(
        agent=spec.name, config=config, provider=provider,
        messages=messages, content=content, node_name=spec.name,
        escalation_provider=_esc_factory if _esc_choice is not None else None,
        on_escalation=_on_escalation if _esc_choice is not None else None)
    # Round outcome for the S14 history: a validation failure past
    # correction is a failure of the model that produced the
    # output; an escalated correction's outcome belongs to the
    # escalated model, and the original keeps its failure.
    _entry_failed = isinstance(structured_entry, dict) and "error" in structured_entry
    if _esc_fired:
        escalated = True
        _record_outcome(provider, "failure")
        _record_outcome(
            _esc_holder.get("provider"),
            "failure" if _entry_failed else "success")
    elif _entry_failed:
        _record_outcome(provider, "failure")
    else:
        _record_outcome(provider, "success")

    result = {"current_phase": spec.phase}
    # P2c step 2: this round offered tools and called none (a final
    # answer) — the offer still belongs in the record.
    result["delegation_log"] = [_tool_offer_entry(spec.name, offered_names, [])]
    if spec.tail_extras is not None:
        result.update(spec.tail_extras(content))
    result["messages"] = [{"role": "assistant", "content": content}]
    # S1-M6: plan mode writes nothing to disk — the content is already
    # returned inline above. artifact_paths is omitted entirely (no
    # consumer reads it; the executor seeds the channel with {}), so
    # the channel keeps its previous value and its dict typing.
    if not state.get("plan_mode"):
        task_id = state.get("task_id", "default_task")
        artifact_path = save_artifact(task_id, spec.artifact_kind, spec.artifact_filename, content)
        result["artifact_paths"] = {spec.artifact_key: artifact_path}
    if structured_entry is not None:
        result["structured_outputs"] = {spec.name: structured_entry}
    return result
