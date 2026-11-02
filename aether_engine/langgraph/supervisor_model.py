"""Routing-model adapter + post-model hook for the supervisor graph.

Epic 1 replaced the hand-written supervisor routing loop with
LangGraph's prebuilt ``create_supervisor()``. Aether's providers are
not LangChain chat models, so this module adapts them:

- ``AetherSupervisorModel`` is a ``BaseChatModel`` whose generation
  calls the resolved Aether provider (``agent_models["supervisor"]``
  override honoured, via the same resolver the supervisor node uses).
  It speaks the supervisor's established decision protocol on top of
  the agent framework:

  - native transfer-tool calls (``transfer_to_<specialist>``) pass
    through and become real handoffs;
  - memory-tool calls pass through to the agent's ToolNode, which runs
    the same audited memory execution path as before;
  - providers that answer with the supervisor's JSON decision text
    (the long-standing prompt protocol) are translated into handoff
    tool calls, so routing behaviour is identical for models without
    native tool calling;
  - after a specialist responds, the provider is consulted again
    with that response in view and its decision rules — delegate on
    (multi-specialist chains), or finish (an END decision, or the
    tool-mode plain-text finish). The old node's ``_should_end_task``
    guard, which ended the run on ANY specialist text, broke
    planner -> coder journeys (E1-F01) and survives only for forced
    runs and empty post-specialist replies, where the specialist's
    answer stands as the result.

- ``supervisor_post_model_hook`` reproduces the supervisor node's
  state bookkeeping (delegation_log entry, context_briefing, plan,
  active_specialist) from the routing decision each model step made.
"""

import copy
import json
import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_config
from pydantic import Field

from aether_engine.langgraph.compaction import (
    context_summary_section,
    model_facing_messages,
)
from aether_engine.langgraph.nodes.supervisor import (
    MEMORY_TOOL_NAMES,
    _available_specialists,
    _decision_from_text,
    _resolve_supervisor_provider,
    _supervisor_system_prompt,
    repo_map_context,
)
from aether_engine.langgraph.state import blackboard_briefing, message_dicts
from aether_engine.tools.policy import resolve_policy_by_name

logger = logging.getLogger("aether_engine.langgraph.supervisor_model")

HANDOFF_PREFIX = "transfer_to_"


def _end_result(reason: str, decision: dict | None = None) -> ChatResult:
    """A final supervisor message that ends the agent run."""
    if decision is None:
        decision = {"next": "END", "reason": reason, "briefing": ""}
    message = AIMessage(
        content=reason,
        additional_kwargs={"aether_decision": decision},
    )
    return ChatResult(generations=[ChatGeneration(message=message)])


def _handoff_result(target: str, decision: dict, memory_calls: list | None = None) -> ChatResult:
    """An AIMessage carrying a handoff tool call for ``target``.

    The call id is freshly generated: providers that stream the same
    call in several chunks reuse one id, and the agent's ToolNode
    rejects duplicate ids inside a run.
    """
    tool_calls = [_langchain_tool_call(tc) for tc in (memory_calls or [])]
    tool_calls.append(
        {
            "name": f"{HANDOFF_PREFIX}{target}",
            "args": {},
            "id": f"aether-handoff-{uuid.uuid4().hex}",
            "type": "tool_call",
        }
    )
    message = AIMessage(
        content="",
        tool_calls=tool_calls,
        additional_kwargs={"aether_decision": decision},
    )
    return ChatResult(generations=[ChatGeneration(message=message)])


def _langchain_tool_call(provider_call: Any) -> dict:
    """Convert an Aether provider ToolCall to LangChain tool-call shape.

    The provider contract (``providers/base.py`` ToolCall) exposes the
    arguments as ``args`` — normally a dict; a raw JSON string is
    parsed, anything else degrades to no arguments. (E1-F02: this
    conversion previously read a non-existent ``.arguments``
    attribute, so every native supervisor memory call crashed before
    retrieval.)
    """
    args = provider_call.args
    if not isinstance(args, dict):
        try:
            args = json.loads(args) if args else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
    return {
        "name": provider_call.name,
        "args": args,
        "id": provider_call.call_id,
        "type": "tool_call",
    }


def _specialist_end_result() -> ChatResult:
    """The local end used when a specialist's answer stands as final.

    The message carries no content of its own: the specialist's text
    already in the history is the task's answer, and the
    ``aether_end_guard`` marker tells the post-model hook not to
    overwrite the briefing/plan bookkeeping for it.
    """
    decision = {
        "next": "END",
        "reason": "Specialist has responded; task complete.",
        "briefing": "",
    }
    message = AIMessage(
        content="",
        additional_kwargs={"aether_decision": decision, "aether_end_guard": True},
    )
    return ChatResult(generations=[ChatGeneration(message=message)])


def _chain_end_reason(specialist_texts: list, delegation_cap: int) -> str | None:
    """Why a post-specialist delegation must not run, or None to allow it.

    The no-progress breaker for the consultation the E1-F01 fix
    introduced. It judges only what this seam can actually see: the
    specialist answers in the message record. (The supervisor's own
    handoff messages are consumed by the supervisor framework and
    never re-enter the model's view, and the spawn-policy hook's
    delegation count restarts with them — neither can bound the loop
    from here.) Two shapes mean the chain has stopped progressing:

    - the last two specialist answers are verbatim identical — the
      chain has converged on an answer; delegating again can only
      re-ask for what is already the latest input;
    - the turn has already produced ``delegation_cap`` specialist
      answers — the same budget the spawn policy puts on delegations,
      counted in the units this seam can observe.
    """
    if len(specialist_texts) >= 2 and specialist_texts[-1] == specialist_texts[-2]:
        return "the last two specialist answers are identical"
    if len(specialist_texts) >= delegation_cap:
        return f"the turn already has {delegation_cap} specialist answers"
    return None


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return tool.get("function", {}).get("name") or tool.get("name")
    return getattr(tool, "name", None)


class AetherSupervisorModel(BaseChatModel):
    """Chat-model facade over the Aether provider resolved per run."""

    aether_tools: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "aether-supervisor-adapter"

    def bind_tools(self, tools: list, **kwargs: Any) -> "AetherSupervisorModel":
        bound = copy.copy(self)
        bound.aether_tools = list(tools)
        return bound

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError(
            "AetherSupervisorModel is async-only; Aether providers stream via call_stream."
        )

    async def _call_provider(self, provider: Any, messages: list[dict], tools: list | None):
        """One provider round-trip; returns (text, [ToolCall])."""
        content_parts: list[str] = []
        accumulated: dict[str, Any] = {}
        async for chunk in provider.call_stream(messages, tools=tools, node_name="supervisor"):
            if chunk.text:
                content_parts.append(chunk.text)
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    if tc.call_id not in accumulated:
                        accumulated[tc.call_id] = tc
            if chunk.finish_reason:
                break
        return "".join(content_parts), list(accumulated.values())

    def _offered_tools(self, config: dict, dict_messages: list[dict], available: list[str]) -> list[dict]:
        """Tools the provider may call this step, with Aether gating applied.

        Mirrors the supervisor node's filtering: policy-denied memory
        tools are never offered; memory tools missing from the
        supervisor's allow-list are offered only as annotated
        do-not-use entries once used (so the model learns they are
        disabled); handoff tools exist only for available specialists.
        """
        configurable = config.get("configurable", {})
        user_config = configurable.get("user_config")
        agent_tools = configurable.get("agent_tools", {})
        allowed = agent_tools.get("supervisor") if isinstance(agent_tools, dict) else None

        # E1-F12: the spawn allow-list filters the native handoff
        # offers themselves, not only the routing prompt's text — a
        # denied specialist's transfer tool is never offered, so the
        # model cannot call what the policy would strip afterwards.
        # Mirrors the filter supervisor_routing_prompt applies.
        spawn_allowlist = getattr(user_config, "supervisor_spawn_allowlist", None)
        if spawn_allowlist is not None:
            available = [a for a in available if a in spawn_allowlist]

        used_tools = set()
        for msg in dict_messages:
            for tc in msg.get("tool_calls") or []:
                name = tc.get("function", {}).get("name")
                if name:
                    used_tools.add(name)

        offered: list[dict] = []
        for tool in self.aether_tools:
            name = _tool_name(tool)
            if not name:
                continue
            if name.startswith(HANDOFF_PREFIX):
                if name[len(HANDOFF_PREFIX):] in available:
                    offered.append(convert_to_openai_tool(tool))
                continue
            if name not in MEMORY_TOOL_NAMES:
                continue
            if user_config and resolve_policy_by_name(name, user_config.tool_policies) == "deny":
                continue
            if isinstance(allowed, list) and name not in allowed:
                if name in used_tools:
                    annotated = convert_to_openai_tool(tool)
                    annotated["function"]["description"] = (
                        "DO NOT USE THIS TOOL. IT IS CURRENTLY DISABLED FOR YOUR ROLE."
                    )
                    offered.append(annotated)
                continue
            offered.append(convert_to_openai_tool(tool))
        return offered

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        try:
            config = get_config()
        except Exception:  # noqa: BLE001 — outside a graph run there is no config; provider resolution below then fails loudly, as it should.
            config = {}
        provider = _resolve_supervisor_provider(config)
        configurable = config.get("configurable", {})
        dict_messages = message_dicts(messages)
        available = _available_specialists(config)

        # Has a specialist already produced a text answer since the
        # last user message? That answer is NOT automatically the
        # task's completion (E1-F01): in a multi-specialist chain it
        # is an intermediate product (the planner's plan), and only
        # the supervisor's next decision — made with that answer in
        # view — can say whether the chain continues or is finished.
        # The provider is therefore consulted below like on any other
        # step; only its reply is interpreted with end-of-chain
        # semantics (see _post_specialist_result). The historical
        # local end survives where the specialist's answer must simply
        # stand: a forced run (single-specialist by definition), an
        # empty post-specialist reply, and the no-progress breaker
        # (_chain_end_reason).
        last_user = max(
            (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
            default=-1,
        )
        specialist_texts = [
            msg["content"]
            for msg in dict_messages[last_user + 1:]
            if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls")
        ]
        specialist_responded = bool(specialist_texts)
        # The turn's delegation budget, counted in specialist answers
        # (the loop breaker's yardstick — see _chain_end_reason). The
        # value mirrors the spawn policy's cap; the hook enforces that
        # cap where it can see handoffs, this seam cannot (below).
        _cap = getattr(configurable.get("user_config"),
                       "supervisor_delegation_cap", 8)
        delegation_cap = _cap if isinstance(_cap, int) and _cap >= 0 else 8

        forced_agent = configurable.get("forced_agent")
        if forced_agent and forced_agent in available:
            if specialist_responded:
                return _specialist_end_result()
            decision = {"next": forced_agent, "reason": "Forced via slash command.", "briefing": ""}
            return _handoff_result(forced_agent, decision)

        if not available:
            return _end_result("All specialists are disabled.")

        offered = self._offered_tools(config, dict_messages, available)
        try:
            content, tool_calls = await self._call_provider(provider, dict_messages, offered or None)
        except Exception as e:  # noqa: BLE001 — provider boundary: mirrors the supervisor node, which retries tool failures as a plain text call.
            logger.warning(f"Supervisor adapter call with tools failed: {e}; falling back to text prompt")
            content, tool_calls = await self._call_provider(provider, dict_messages, None)

        memory_calls = [tc for tc in tool_calls if tc.name in MEMORY_TOOL_NAMES]
        transfer_calls = [tc for tc in tool_calls if tc.name.startswith(HANDOFF_PREFIX)]
        # Epic 3, E3-01 (issue 012 diagnosability): calls that are
        # neither memory nor handoff are NOT the supervisor's to run —
        # it is offered only memory + handoff tools, and real tool work
        # belongs to specialists through the central pipeline. They are
        # still dropped, but never silently: the names are logged and
        # recorded on the decision (delegation_log) so a vanished tool
        # call is diagnosable instead of a mystery.
        dropped_calls = [
            tc.name for tc in tool_calls if tc not in memory_calls and tc not in transfer_calls
        ]
        if dropped_calls:
            logger.warning(
                "Supervisor emitted non-supervisor tool calls, dropped (delegate to a specialist instead): %s",
                dropped_calls,
            )

        if transfer_calls:
            target = transfer_calls[0].name[len(HANDOFF_PREFIX):]
            if specialist_responded:
                chain_end = _chain_end_reason(specialist_texts, delegation_cap)
                if chain_end is not None:
                    logger.warning(
                        "Post-specialist delegation to %s refused: %s; "
                        "ending with the latest answer standing.",
                        target, chain_end)
                    return _specialist_end_result()
            decision = {"next": target, "reason": (content or "").strip(), "briefing": ""}
            if dropped_calls:
                decision["dropped_tool_calls"] = dropped_calls
            if target not in available:
                # Routing clamps to END, exactly like the old node; the
                # attempted decision is still what gets logged (hook).
                return _end_result(decision["reason"], decision=decision)
            return _handoff_result(target, decision, memory_calls)

        if memory_calls:
            message = AIMessage(
                content=content or "",
                tool_calls=[_langchain_tool_call(tc) for tc in memory_calls],
                additional_kwargs={"aether_dropped_tool_calls": dropped_calls} if dropped_calls else {},
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        if specialist_responded:
            return self._post_specialist_result(
                content, available, dropped_calls,
                specialist_texts, delegation_cap)

        decision = _decision_from_text(content or "", available)
        if not isinstance(decision, dict):
            decision = {
                "next": "planner" if "planner" in available else available[0],
                "reason": f"Failed to parse supervisor decision: {(content or '')[:100]}",
                "briefing": "",
            }
        if dropped_calls:
            decision["dropped_tool_calls"] = dropped_calls
        next_node = decision.get("next", "END")
        if next_node != "END" and next_node in available:
            return _handoff_result(next_node, decision)
        return _end_result(decision.get("reason") or content or "", decision=decision)

    @staticmethod
    def _post_specialist_result(content, available, dropped_calls,
                                specialist_texts, delegation_cap) -> ChatResult:
        """Interpret the supervisor's reply after a specialist responded.

        The supervisor has the specialist's output in view, so its
        decision rules (E1-F01): a JSON decision delegates on or ends,
        exactly as on the first hop; plain text with no tool calls is
        the tool-mode finish the routing prompt teaches ("reply with
        plain text and call no tool"); an empty reply keeps the
        historical local end, with the specialist's answer standing as
        the result. The first-hop fallback in ``_decision_from_text``
        (unparseable text delegates to the planner) must NOT apply
        here — applied after a completed hop it would resurrect the
        finished chain as a delegation loop.

        Delegations are additionally subject to the no-progress
        breaker (``_chain_end_reason``): without it, a supervisor
        stuck delegating onward — classically straight back to the
        specialist that just answered — ping-pongs until the graph
        recursion limit fails the whole task.
        """
        text = (content or "").strip()
        if not text:
            return _specialist_end_result()
        cleaned = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = None
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            decision = parsed
            if dropped_calls:
                decision["dropped_tool_calls"] = dropped_calls
            next_node = decision.get("next", "END")
            if next_node != "END" and next_node in available:
                chain_end = _chain_end_reason(specialist_texts, delegation_cap)
                if chain_end is not None:
                    logger.warning(
                        "Post-specialist delegation to %s refused: %s; "
                        "ending with the latest answer standing.",
                        next_node, chain_end)
                    return _specialist_end_result()
                return _handoff_result(next_node, decision)
            return _end_result(decision.get("reason") or text, decision=decision)
        decision = {"next": "END", "reason": text, "briefing": ""}
        if dropped_calls:
            decision["dropped_tool_calls"] = dropped_calls
        return _end_result(text, decision=decision)


def supervisor_routing_prompt(state: dict) -> list:
    """create_supervisor prompt callable: the routing prompt, tool mode.

    A callable prompt in this langgraph-prebuilt version returns the
    COMPLETE model input, so the state history is appended after the
    system message.
    """
    try:
        config = get_config()
    except Exception:  # noqa: BLE001 — prompt building must not fail the run; all specialists are offered when config is unavailable.
        config = {}
    available = _available_specialists(config)
    # E1-11: don't even offer specialists the spawn policy would deny
    # (the post-model hook still enforces it for native tool calls).
    _allowlist = getattr(
        (config.get("configurable", {}) or {}).get("user_config"),
        "supervisor_spawn_allowlist", None)
    if _allowlist is not None:
        available = [a for a in available if a in _allowlist]
    text = _supervisor_system_prompt(
        state.get("original_prompt", "None"),
        state.get("plan", "No plan yet."),
        available,
        tool_mode=True,
    )
    board = blackboard_briefing(state)
    if board:
        text += f"\n\nShared team blackboard (written by all agents in this task):\n{board}"
    summary_section = context_summary_section(state)
    if summary_section:
        text += f"\n\n{summary_section}"
    # E8-3.1: ambient repo map (graph tasks only) — built by
    # nodes/supervisor.repo_map_context from the run's granted
    # workspace roots, ranked toward this task.
    repo_section = repo_map_context(
        config,
        state.get("original_prompt", ""),
        " ".join(
            str(m.get("content", ""))
            for m in message_dicts(state.get("messages", []))
        ),
    )
    if repo_section:
        text += f"\n\n{repo_section}"
    # E1-06: the routing model sees the compacted view (summary above +
    # verbatim tail), never the folded middle turns. The state channel
    # itself keeps the full history.
    return [SystemMessage(content=text), *model_facing_messages(state)]


def _spawn_policy_denial(state: dict, target: str, config: dict | None = None) -> dict | None:
    """Spawn policy (Epic 1, E1-11): a denial update, or None to allow.

    Enforced at the last moment before a handoff would execute. On a
    denial the pending message's transfer call is stripped (same
    message id, so it replaces in place) and an explanation with no
    tool calls is appended, which ends the supervisor's turn: the
    delegation never happens, the denial is logged, and the reason
    becomes the run's final message instead of a silent drop.
    """
    # E1-F11: plan mode disables supervisor spawns beyond planning —
    # the flag lives in state, so this check needs no config and
    # covers every handoff shape (native, forced, JSON-decoded) that
    # reaches this backstop. Only delegation to the planner survives:
    # a plan-mode run still needs its planning step, while execution
    # specialists must never start.
    reason = None
    if state.get("plan_mode") and target != "planner":
        reason = (
            "plan mode is active: supervisor spawns are disabled in "
            "plan mode — only delegation to the planner is allowed"
        )
    if reason is None:
        if config is None:
            try:
                config = get_config()
            except Exception:  # noqa: BLE001 — policy must never break routing when config is unavailable.
                return None
        user_config = (config.get("configurable", {}) or {}).get("user_config")
        if user_config is None:
            return None
        allowlist = getattr(user_config, "supervisor_spawn_allowlist", None)
        cap = getattr(user_config, "supervisor_delegation_cap", 8)
        if allowlist is not None and target not in allowlist:
            reason = f"'{target}' is not in the supervisor spawn allow-list ({', '.join(allowlist) or 'empty'})"
        elif isinstance(cap, int) and cap >= 0:
            dict_messages = message_dicts(state.get("messages", []))
            last_user = max(
                (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
                default=-1,
            )
            delegations = 0
            for msg in dict_messages[last_user + 1:]:
                for tc in msg.get("tool_calls") or []:
                    if (tc.get("function", {}).get("name") or "").startswith(HANDOFF_PREFIX):
                        delegations += 1
            # The count includes the pending handoff (the last message).
            if delegations > cap:
                reason = f"the delegation cap for one turn ({cap}) is reached"
    if reason is None:
        return None
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    # A SINGLE replacement message, never a batch: multi-message writes
    # to this channel are normalised to plain dicts by the parent
    # state's reducer, which the supervisor agent's internal tool
    # routing cannot read (state.py, collapse_subgraph_echo). With the
    # transfer stripped, the agent ends its turn and the explanation
    # becomes the final message; the executor surfaces denied decisions
    # from the delegation log as the task's response text.
    explanation = (
        f"Delegation to {target} was blocked by the spawn policy: {reason}. "
        "No specialist was started. Adjust supervisor_spawn_allowlist / "
        "supervisor_delegation_cap in the config, or ask for the work to be "
        "handled another way."
    )
    stripped = AIMessage(content=explanation, id=getattr(last, "id", None))
    decision: dict[str, Any] = {
        "next": target, "denied": True, "reason": reason, "briefing": "",
    }
    # E3-F10: the denial replaces the pending message wholesale, and
    # the fresh decision above would take the routing step's drop
    # metadata with it — the non-supervisor calls emitted alongside
    # the handoff (stamped on the decision as dropped_tool_calls)
    # vanished from the latest state and the delegation log. Carry
    # them into the denial decision, where the log keeps them.
    stamped = (getattr(last, "additional_kwargs", None) or {}).get("aether_decision")
    if isinstance(stamped, dict) and stamped.get("dropped_tool_calls"):
        decision["dropped_tool_calls"] = list(stamped["dropped_tool_calls"])
    return {
        "messages": [stripped],
        "delegation_log": [
            {"agent": "supervisor", "timestamp": time.time(),
             "decision": decision}
        ],
        "active_specialist": None,
    }


def supervisor_post_model_hook(state: dict) -> dict:
    """State bookkeeping for one supervisor model step.

    Reproduces the supervisor node's returns: a delegation_log entry
    for every decision, plus context_briefing / plan / active_specialist
    updates. Memory-tool steps change nothing (the model decides again
    once the results land).
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage):
        return {}
    tool_calls = last.tool_calls or []
    handoffs = [tc for tc in tool_calls if (tc.get("name") or "").startswith(HANDOFF_PREFIX)]
    additional = last.additional_kwargs or {}
    decision = additional.get("aether_decision")

    if handoffs:
        target = handoffs[0]["name"][len(HANDOFF_PREFIX):]
        denial = _spawn_policy_denial(state, target)
        if denial is not None:
            return denial
        if decision is None or (
            isinstance(decision, dict) and not decision.get("briefing")
        ):
            # Briefing reconstruction: the briefing is what memory
            # tools returned since the last user message. This used to
            # run only for unstamped (decision-less) handoffs — but
            # native transfers stamp a decision with an EMPTY briefing
            # (E1-F09), which silently bypassed the reconstruction and
            # handed the specialist no retrieved context. A stamped
            # decision with no briefing of its own now receives the
            # same reconstruction; its next/reason are untouched, and
            # a decision carrying a real briefing keeps it.
            briefing_parts = []
            dict_messages = message_dicts(messages)
            last_user = max(
                (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
                default=-1,
            )
            for msg in dict_messages[last_user + 1:]:
                if msg.get("role") == "tool" and msg.get("content"):
                    briefing_parts.append(msg["content"])
            reconstructed = "\n".join(briefing_parts)
            if decision is None:
                # Native-tool routing: the reason is the model's own
                # reply text alongside the transfer call.
                decision = {
                    "next": target,
                    "reason": last.content if isinstance(last.content, str) else "",
                    "briefing": reconstructed,
                }
            elif reconstructed:
                decision = {**decision, "briefing": reconstructed}
        reason = decision.get("reason", "")
        return {
            "delegation_log": [
                {"agent": "supervisor", "timestamp": time.time(), "decision": decision}
            ],
            "context_briefing": decision.get("briefing", "") or "",
            "plan": f"Goal: {reason}",
            "active_specialist": target,
        }

    if tool_calls:
        # Memory step: the model decides again once the results land,
        # so routing bookkeeping does not change — with one exception
        # (E3-F10). When the routing step dropped non-supervisor calls
        # alongside the memory calls, those names exist only on this
        # message; returning nothing here lost them from the
        # delegation log and (once the message was normalized) from
        # the latest state. Record them the way other decisions are
        # recorded.
        dropped = additional.get("aether_dropped_tool_calls")
        if dropped:
            return {
                "delegation_log": [
                    {"agent": "supervisor", "timestamp": time.time(),
                     "decision": {
                         "next": "supervisor",
                         "reason": "Memory step: supervisor called memory tools directly.",
                         "briefing": "",
                         "dropped_tool_calls": list(dropped),
                     }}
                ]
            }
        return {}

    if decision is None:
        decision = {
            "next": "END",
            "reason": last.content if isinstance(last.content, str) else "",
            "briefing": "",
        }
    update: dict[str, Any] = {
        "delegation_log": [
            {"agent": "supervisor", "timestamp": time.time(), "decision": decision}
        ],
        "active_specialist": None,
    }
    if not additional.get("aether_end_guard"):
        # The end-guard message stands in for the specialist's own
        # completion update; every other final supervisor message is a
        # routing decision and records its plan like the node did.
        update["context_briefing"] = decision.get("briefing", "") or ""
        update["plan"] = f"Goal: {decision.get('reason', '')}"
    return update
