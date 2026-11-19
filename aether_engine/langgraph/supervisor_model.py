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
    through and become real handoffs — under the P1d
    subagents-as-tools supervisor the same role is played by native
    delegation-tool calls (``delegate_to_<specialist>``), which pass
    through with their briefing argument intact;
  - memory-tool calls pass through to the agent's ToolNode, which runs
    the same audited memory execution path as before;
  - providers that answer with the supervisor's JSON decision text
    (the long-standing prompt protocol) are translated into handoff
    tool calls, so routing behaviour is identical for models without
    native tool calling;
  - after a specialist responds, the provider is consulted again
    with that response in view and its decision rules — delegate on
    (multi-specialist chains), or finish (an END decision, or the
    tool-mode plain-text finish). The old node's end-task guard,
    which ended the run on ANY specialist text, broke
    planner -> coder journeys (E1-F01); its surviving form covers
    only forced runs and empty post-specialist replies, where the
    specialist's answer stands as the result.

- ``supervisor_post_model_hook`` reproduces the supervisor node's
  state bookkeeping (delegation_log entry, context_briefing, plan,
  active_specialist) from the routing decision each model step made.
"""

import copy
import json
import logging
import os
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_config
from pydantic import Field

from aether_engine.delegation_settings import resolve_delegation_settings
from aether_engine.desktop import observe_permitted
from aether_engine.langgraph.compaction import (
    context_summary_section,
    model_facing_messages,
)
from aether_engine.langgraph.nodes.supervisor import (
    MEMORY_TOOL_NAMES,
    SCREEN_TOOL_NAMES,
    _available_specialists,
    _decision_from_text,
    _resolve_supervisor_provider,
    _supervisor_system_prompt,
    repo_map_context,
)
from aether_engine.langgraph.state import (
    blackboard_briefing,
    delegation_briefing,
    message_dicts,
)
from aether_engine.providers.base import ProviderError
from aether_engine.tools.policy import resolve_policy_by_name

logger = logging.getLogger("aether_engine.langgraph.supervisor_model")

HANDOFF_PREFIX = "transfer_to_"

# P1d: the subagents-as-tools supervisor's delegation calls. The
# adapter speaks both protocols while both graph topologies exist:
# which one a run uses is decided by the tools bound into the agent
# (transfer tools under create_supervisor, delegation tools under
# langgraph/delegation.py), never by config sniffing here.
DELEGATION_PREFIX = "delegate_to_"


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


def _provider_call_args(provider_call: Any) -> dict:
    """A provider ToolCall's arguments as a dict (the same
    normalisation ``_langchain_tool_call`` applies)."""
    args = provider_call.args
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args) if args else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _delegation_result(
    target: str,
    decision: dict,
    memory_calls: list | None = None,
    provider_call: Any = None,
) -> ChatResult:
    """An AIMessage carrying a delegation tool call for ``target``.

    The P1d counterpart of ``_handoff_result``: the call is
    ``delegate_to_<target>`` and carries the decision's briefing as
    its ``briefing`` argument (the seam P1a's tier emission later
    targets). A native provider call passes through with its own
    id and arguments — the model authored both — while a call the
    adapter synthesises (JSON decision protocol, forced runs) gets
    a fresh id, exactly as handoffs do: providers that stream one
    call in several chunks reuse an id, and the agent's ToolNode
    rejects duplicate ids inside a run.
    """
    tool_calls = [_langchain_tool_call(tc) for tc in (memory_calls or [])]
    if provider_call is not None:
        tool_calls.append(_langchain_tool_call(provider_call))
    else:
        tool_calls.append(
            {
                "name": f"{DELEGATION_PREFIX}{target}",
                "args": {"briefing": decision.get("briefing", "") or ""},
                "id": f"aether-delegation-{uuid.uuid4().hex}",
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


def _chain_end_reason(specialist_texts: list) -> str | None:
    """Why a post-specialist delegation must not run, or None to allow it.

    The no-progress breaker for the consultation the E1-F01 fix
    introduced. It judges only progress, in the one unit this seam
    can actually see: the specialist answers in the message record.
    The chain has stopped progressing when the last two specialist
    answers are verbatim identical — the chain has converged on an
    answer; delegating again can only re-ask for what is already the
    latest input.

    The delegation *cap* is deliberately NOT judged here any more
    (master build S4): this seam cannot see the authoritative
    delegation counter (the supervisor's handoff messages are
    consumed by the framework and never re-enter the model's view,
    so a count derived here was never the real count — audit G10).
    The cap is enforced against ``state["delegation_count"]`` at the
    post-model hook, the single place a delegation actually happens:
    ``_spawn_policy_denial`` for the denial surface, and
    ``_cap_end_update`` for the post-specialist surface this breaker
    used to produce (latest answer standing, no denial text).
    """
    if len(specialist_texts) >= 2 and specialist_texts[-1] == specialist_texts[-2]:
        return "the last two specialist answers are identical"
    return None


def _delegation_count(state: dict) -> int:
    """The authoritative delegation count for this turn (S4).

    ``state["delegation_count"]`` is the counter of record, written
    by the post-model hook at the moment each handoff executes.
    States that predate the counter (a checkpoint resumed across the
    S4 boundary, or a hand-built state in a direct policy call) fall
    back to the delegation log — the checkpointed record of the same
    events, counting only real delegations (not END decisions, not
    memory steps, not denied attempts). A state with neither falls
    back to handoff calls in its messages, excluding the pending
    (last) handoff message, which has not executed yet.
    """
    count = (state or {}).get("delegation_count")
    if isinstance(count, int) and count >= 0:
        return count
    log_count = 0
    seen_log = False
    for entry in (state or {}).get("delegation_log") or []:
        if not isinstance(entry, dict):
            continue
        seen_log = True
        decision = entry.get("decision") or {}
        target = decision.get("next") if isinstance(decision, dict) else None
        if not target or str(target).upper() == "END" or str(target) == "supervisor":
            continue
        if isinstance(decision, dict) and decision.get("denied"):
            continue
        log_count += 1
    if seen_log:
        return log_count
    dict_messages = message_dicts((state or {}).get("messages", []))
    last_user = max(
        (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
        default=-1,
    )
    handoffs = 0
    for msg in dict_messages[last_user + 1:]:
        for tc in msg.get("tool_calls") or []:
            if (tc.get("function", {}).get("name") or "").startswith(
                (HANDOFF_PREFIX, DELEGATION_PREFIX)
            ):
                handoffs += 1
    # The pending handoff (the one being judged) is in the messages
    # but has not executed; the executed count excludes it.
    return max(0, handoffs - 1)


def _delegation_cap(config: dict | None = None) -> int:
    """The configured per-turn delegation cap (default 8).

    P1e: read through the delegation-settings resolver — the
    single read path for the cap (the resolver composes it from
    ``supervisor_delegation_cap``, its storage field).
    """
    if config is None:
        try:
            config = get_config()
        except Exception:  # noqa: BLE001 — outside a graph run there is no config; the default cap applies.
            config = {}
    user_config = ((config or {}).get("configurable", {}) or {}).get("user_config")
    return resolve_delegation_settings(user_config).max_delegations_per_turn


def _specialist_answers(state: dict) -> list:
    """Specialist text answers since the last user message.

    The same derivation the model adapter applies to its message
    view: assistant messages with content and no tool calls, after
    the last user message.
    """
    dict_messages = message_dicts((state or {}).get("messages", []))
    last_user = max(
        (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
        default=-1,
    )
    return [
        msg["content"]
        for msg in dict_messages[last_user + 1:]
        if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls")
    ]


def _cap_end_update(state: dict) -> dict:
    """End a capped post-specialist chain with the latest answer standing.

    This is the state update equivalent of the model adapter's
    ``_specialist_end_result`` (master build S4): the pending handoff
    message is replaced in place (same message id) by an empty
    end-guard message, so the supervisor's turn ends, the specialist's
    answer already in the history stands as the result, and the
    delegation log records the same END decision the model-produced
    end records — no denial entry, no denial text. Used only when a
    specialist answer exists to stand on; a cap hit with no answer
    yet (e.g. cap 0 on the first hop) is the spawn policy's denial
    surface instead.
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    decision = {
        "next": "END",
        "reason": "Specialist has responded; task complete.",
        "briefing": "",
    }
    stripped = AIMessage(
        content="",
        id=getattr(last, "id", None),
        additional_kwargs={"aether_decision": decision, "aether_end_guard": True},
    )
    return {
        "messages": [stripped],
        "delegation_log": [
            {"agent": "supervisor", "timestamp": time.time(), "decision": decision}
        ],
        "active_specialist": None,
    }


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

    def _uses_delegation_tools(self) -> bool:
        """True when this agent carries P1d delegation tools.

        The bound tools are the topology's ground truth: the legacy
        graph binds ``transfer_to_*`` handoff tools, the
        subagents-as-tools graph binds ``delegate_to_*`` tools, and
        the decision protocol below speaks whichever the run was
        built with.
        """
        return any(
            (_tool_name(t) or "").startswith(DELEGATION_PREFIX)
            for t in self.aether_tools
        )

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
            if name.startswith(DELEGATION_PREFIX):
                if name[len(DELEGATION_PREFIX):] in available:
                    offered.append(convert_to_openai_tool(tool))
                continue
            # S19: the desktop-observe tool exists for the model
            # only when Computer Use + Observe are enabled —
            # never offered (and never annotated as disabled)
            # otherwise, so the offered list with Computer Use
            # off is byte-identical to pre-S19.
            if name in SCREEN_TOOL_NAMES and not observe_permitted(user_config):
                continue
            if name not in MEMORY_TOOL_NAMES and name not in SCREEN_TOOL_NAMES:
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
        # The turn's delegation cap is NOT judged at this seam (S4):
        # the model cannot see the authoritative delegation counter,
        # so the cap is enforced at the post-model hook against
        # state["delegation_count"] — see _chain_end_reason.

        forced_agent = configurable.get("forced_agent")
        if forced_agent and forced_agent in available:
            if specialist_responded:
                return _specialist_end_result()
            decision = {"next": forced_agent, "reason": "Forced via slash command.", "briefing": ""}
            if self._uses_delegation_tools():
                return _delegation_result(forced_agent, decision)
            return _handoff_result(forced_agent, decision)

        if not available:
            return _end_result("All specialists are disabled.")

        offered = self._offered_tools(config, dict_messages, available)
        try:
            content, tool_calls = await self._call_provider(provider, dict_messages, offered or None)
        except ProviderError:
            # N-22: a typed provider failure (auth, rate-limit,
            # timeout) is the graph/pump's signal, not a tool-mode
            # problem — the text-prompt retry cannot fix it and only
            # adds a second billed call (and worsens a rate limit).
            # Propagate immediately, exactly as the retry path below
            # already treats typed failures on the second attempt.
            raise
        except Exception as e:  # noqa: BLE001 — provider boundary: mirrors the supervisor node, which retries tool failures as a plain text call.
            logger.warning(f"Supervisor adapter call with tools failed: {e}; falling back to text prompt")
            try:
                content, tool_calls = await self._call_provider(provider, dict_messages, None)
            except ProviderError:
                # Typed provider failures are the graph/pump's
                # signal, not a routing outcome (F09: TASK_FAILED
                # carries the retriable classification the task-level
                # fallback reads). Degrading one to an END would
                # report a task that never ran as completed — let it
                # propagate.
                raise
            except Exception as retry_error:  # noqa: BLE001 — S1-N5: the tool-less retry was unguarded — an unexpected failure on both attempts escaped raw and killed the routing step. Degrade to an end result instead.
                logger.warning(f"Supervisor adapter text fallback also failed: {retry_error}")
                return _end_result(f"Supervisor model call failed: {retry_error}")

        # The supervisor's OWN tools: the memory tools plus
        # (S19) the desktop-observe tool — both execute inside
        # the supervisor via its ToolNode, under their gates.
        memory_calls = [tc for tc in tool_calls
                        if tc.name in MEMORY_TOOL_NAMES
                        or tc.name in SCREEN_TOOL_NAMES]
        transfer_calls = [tc for tc in tool_calls if tc.name.startswith(HANDOFF_PREFIX)]
        delegate_calls = [tc for tc in tool_calls if tc.name.startswith(DELEGATION_PREFIX)]
        # Epic 3, E3-01 (issue 012 diagnosability): calls that are
        # neither memory nor handoff are NOT the supervisor's to run —
        # it is offered only memory + handoff tools, and real tool work
        # belongs to specialists through the central pipeline. They are
        # still dropped, but never silently: the names are logged and
        # recorded on the decision (delegation_log) so a vanished tool
        # call is diagnosable instead of a mystery.
        dropped_calls = [
            tc.name for tc in tool_calls
            if tc not in memory_calls and tc not in transfer_calls
            and tc not in delegate_calls
        ]
        if dropped_calls:
            logger.warning(
                "Supervisor emitted non-supervisor tool calls, dropped (delegate to a specialist instead): %s",
                dropped_calls,
            )

        if delegate_calls:
            # P1d native protocol: the model's own delegation call
            # passes through with its id and its briefing argument;
            # the decision is stamped from it, so the post-model
            # hook's bookkeeping (and its briefing rule — a decision
            # carrying a real briefing keeps it) works unchanged.
            call = delegate_calls[0]
            target = call.name[len(DELEGATION_PREFIX):]
            if specialist_responded:
                chain_end = _chain_end_reason(specialist_texts)
                if chain_end is not None:
                    logger.warning(
                        "Post-specialist delegation to %s refused: %s; "
                        "ending with the latest answer standing.",
                        target, chain_end)
                    return _specialist_end_result()
            decision = {
                "next": target,
                "reason": (content or "").strip(),
                "briefing": _provider_call_args(call).get("briefing", "") or "",
            }
            if dropped_calls:
                decision["dropped_tool_calls"] = dropped_calls
            if target not in available:
                # Routing clamps to END, exactly like the old node; the
                # attempted decision is still what gets logged (hook).
                return _end_result(decision["reason"], decision=decision)
            return _delegation_result(
                target, decision, memory_calls, provider_call=call)

        if transfer_calls:
            target = transfer_calls[0].name[len(HANDOFF_PREFIX):]
            if specialist_responded:
                chain_end = _chain_end_reason(specialist_texts)
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
                content, available, dropped_calls, specialist_texts,
                delegation=self._uses_delegation_tools())

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
            if self._uses_delegation_tools():
                return _delegation_result(next_node, decision)
            return _handoff_result(next_node, decision)
        return _end_result(decision.get("reason") or content or "", decision=decision)

    @staticmethod
    def _post_specialist_result(content, available, dropped_calls,
                                specialist_texts, delegation: bool = False) -> ChatResult:
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
                chain_end = _chain_end_reason(specialist_texts)
                if chain_end is not None:
                    logger.warning(
                        "Post-specialist delegation to %s refused: %s; "
                        "ending with the latest answer standing.",
                        next_node, chain_end)
                    return _specialist_end_result()
                if delegation:
                    return _delegation_result(next_node, decision)
                return _handoff_result(next_node, decision)
            return _end_result(decision.get("reason") or text, decision=decision)
        decision = {"next": "END", "reason": text, "briefing": ""}
        if dropped_calls:
            decision["dropped_tool_calls"] = dropped_calls
        return _end_result(text, decision=decision)


def _eq_task_outcomes(state: dict) -> list[str]:
    """Within-task outcome labels for the EQ failure signals.

    Only what this run genuinely holds: tool results in the
    message channel (a tool message whose status says error is a
    failure; any other tool result is a completion) and denied
    delegations from the delegation log. Previous tasks' outcomes
    live in episodic memory, which the EQ path must never touch
    (aether_engine/eq.py: transient by construction) — so the
    streak this sees is the current task's, and the
    conversation-level signals come from the user's own words in
    the session history instead.
    """
    outcomes: list[str] = []
    for message in state.get("messages") or []:
        if isinstance(message, dict):
            role = message.get("role")
            status = message.get("status")
        else:
            role = getattr(message, "type", None)
            status = getattr(message, "status", None)
        if role in ("tool", "function"):
            outcomes.append(
                "failed" if status == "error" else "succeeded")
    for entry in state.get("delegation_log") or []:
        if not isinstance(entry, dict):
            continue
        decision = entry.get("decision") or {}
        if isinstance(decision, dict) and decision.get("denied"):
            outcomes.append("failed")
    return outcomes


def _eq_briefing_for_state(state: dict, config: dict) -> str:
    """The EQ briefing section for the routing prompt, or "".

    Master-build S11: the supervisor routes — and writes the
    final answer — with behaviour guidance from the
    conversation's signals (aether_engine/eq.py), under the same
    briefing contract as the blackboard/delegation sections:
    bounded, empty when the read is neutral or low-confidence,
    and it NEVER breaks routing (any failure is an empty
    section). Inputs are only what the run already holds: the
    session's user turns (via fast chat's shared history loader)
    plus the current request, and this task's own tool/delegation
    outcomes. The memory facade is never touched — the read is
    transient and is persisted nowhere.
    """
    try:
        user_config = (config.get("configurable", {}) or {}).get(
            "user_config")
        if user_config is not None and not getattr(
                user_config, "eq_enabled", True):
            return ""
        from aether_engine.eq import detect_affect, eq_briefing
        from aether_engine.langgraph.fast_chat import (
            load_session_history,
        )

        original_prompt = str(state.get("original_prompt") or "")
        history = load_session_history(
            state.get("session_id"), original_prompt)
        user_messages = [
            str(m.get("content"))
            for m in history
            if isinstance(m, dict) and m.get("role") == "user"
            and isinstance(m.get("content"), str)
        ]
        if original_prompt:
            user_messages.append(original_prompt)
        if not user_messages:
            # Fresh thread with no session behind it: the state
            # channel's own user turns are what the run holds.
            user_messages = [
                str(d.get("content"))
                for d in message_dicts(state.get("messages", []))
                if isinstance(d, dict) and d.get("role") == "user"
                and isinstance(d.get("content"), str)
            ]
        read = detect_affect(user_messages, _eq_task_outcomes(state))
        return eq_briefing(read)
    except Exception:  # EQ must never break routing
        logger.debug("Supervisor EQ briefing unavailable", exc_info=True)
        return ""


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
    # P1d: the prompt teaches the delegation protocol the run's
    # graph was built with — resolved from the same sources as
    # graph.resolve_supervisor_pattern (the environment override
    # first, then the run's UserConfig field), minus the
    # compile-time argument the prompt cannot see.
    _pattern = os.environ.get("AETHER_SUPERVISOR_PATTERN") or getattr(
        (config.get("configurable", {}) or {}).get("user_config"),
        "supervisor_pattern", None)
    text = _supervisor_system_prompt(
        state.get("original_prompt", "None"),
        state.get("plan", "No plan yet."),
        available,
        tool_mode=True,
        delegation_tools=_pattern == "tools",
    )
    board = blackboard_briefing(state)
    if board:
        text += f"\n\nShared team blackboard (written by all agents in this task):\n{board}"
    # S4: delegation read-back — the supervisor routes with its own
    # delegation history in view (audit G7: delegation_log was
    # write-only for the model). Same briefing contract as the
    # blackboard: bounded, empty when nothing has been delegated.
    delegations = delegation_briefing(state)
    if delegations:
        text += f"\n\nDelegation history for this task:\n{delegations}"
    # S11: EQ briefing — behaviour guidance from the
    # conversation's signals, beside the delegation read-back
    # under the same contract (bounded, empty when neutral or
    # low-confidence, never breaks routing). It shapes how the
    # supervisor works and phrases the final answer; it is not
    # a fact about the user and is persisted nowhere.
    eq_section = _eq_briefing_for_state(state, config)
    if eq_section:
        text += f"\n\n{eq_section}"
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
    # S19: desktop observe — the section exists only when the
    # user has enabled Computer Use + Observe (the same gate
    # that offers the tool); with it off the prompt is
    # byte-identical to pre-S19. Pull-only discipline, like the
    # memory tools: the screen is read when the task is about
    # the screen, never ambiently.
    _user_config = (config.get("configurable", {}) or {}).get("user_config")
    if observe_permitted(_user_config):
        text += (
            "\n\nYou have a desktop observation tool:\n"
            "- get_screen_state: read the user's foreground window as "
            "accessibility TEXT (window title, app, and the element "
            "tree — names, roles, values, the focused element marked) "
            "plus the list of visible windows. Call it only when the "
            "task is about what is on the user's screen or which "
            "app/window they are working in. It reads text only — it "
            "cannot click, type, or take screenshots, and nothing is "
            "watched continuously."
        )
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
        # P1e: the cap comes from the delegation-settings resolver
        # (the single read path), as in _delegation_cap above.
        cap = resolve_delegation_settings(user_config).max_delegations_per_turn
        if allowlist is not None and target not in allowlist:
            reason = f"'{target}' is not in the supervisor spawn allow-list ({', '.join(allowlist) or 'empty'})"
        # S4: the cap is judged against the authoritative delegation
        # counter (see _delegation_count), not a count derived from
        # message tool calls — handoff messages are consumed by the
        # framework and never re-enter the model's view, so the
        # derived count restarted with them (audit G10). The counter
        # holds delegations that have actually executed; the pending
        # handoff would be the next one, so it is denied when the
        # counter already stands at the cap.
        elif isinstance(cap, int) and cap >= 0 and _delegation_count(state) >= cap:
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
    # A pending delegation, in either topology's protocol: the
    # legacy transfer call or the P1d delegation call. Everything
    # below (cap surface, spawn policy, briefing reconstruction,
    # the counter increment) is protocol-independent — it judges
    # the decision, not the call's shape.
    handoffs = [
        tc for tc in tool_calls
        if (tc.get("name") or "").startswith((HANDOFF_PREFIX, DELEGATION_PREFIX))
    ]
    additional = last.additional_kwargs or {}
    decision = additional.get("aether_decision")

    if handoffs:
        pending_name = handoffs[0]["name"]
        target = pending_name[
            len(HANDOFF_PREFIX) if pending_name.startswith(HANDOFF_PREFIX)
            else len(DELEGATION_PREFIX):
        ]
        # S4 cap surface, judged BEFORE the spawn-policy denial: when
        # the authoritative counter already stands at the cap AND a
        # specialist answer is in view, the chain ends with that
        # answer standing — the surface _chain_end_reason's cap branch
        # produced at the model seam before S4 moved cap enforcement
        # to the counter. (The breaker at the model seam runs first
        # there too: a capped post-specialist handoff never reached
        # this hook's denial as a denial.) A cap hit with no answer
        # standing — cap 0 on the first hop — falls through to the
        # denial below, exactly as before.
        if _specialist_answers(state) and _delegation_count(state) >= _delegation_cap():
            logger.warning(
                "Post-specialist delegation to %s refused: the turn "
                "already has %s delegations (the cap); ending with "
                "the latest answer standing.",
                target, _delegation_count(state))
            return _cap_end_update(state)
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
            # S4: THE single increment of the authoritative counter.
            # This return is the one place a handoff that has passed
            # the spawn policy becomes a delegation that will execute
            # (the framework routes to the specialist from here), so
            # it is the one place the counter moves. Denials returned
            # above, END decisions and memory steps never reach it.
            "delegation_count": _delegation_count(state) + 1,
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
