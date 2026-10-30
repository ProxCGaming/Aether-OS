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
  - when a specialist has already produced the task's answer, the
    adapter ends the run locally instead of asking the provider to
    route again (the old node's ``_should_end_task`` guard, applied
    adapter-side because this langgraph-prebuilt version has no
    model-level jump_to support).

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

from aether_engine.langgraph.nodes.supervisor import (
    MEMORY_TOOL_NAMES,
    _available_specialists,
    _decision_from_text,
    _resolve_supervisor_provider,
    _supervisor_system_prompt,
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
    """Convert an Aether provider ToolCall to LangChain tool-call shape."""
    args = provider_call.arguments
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

        # A specialist's final text answer (after the last user message)
        # completes the task: end locally instead of routing again.
        last_user = max(
            (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
            default=-1,
        )
        for msg in dict_messages[last_user + 1:]:
            if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
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

        forced_agent = configurable.get("forced_agent")
        if forced_agent and forced_agent in available:
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

        if transfer_calls:
            target = transfer_calls[0].name[len(HANDOFF_PREFIX):]
            decision = {"next": target, "reason": (content or "").strip(), "briefing": ""}
            if target not in available:
                # Routing clamps to END, exactly like the old node; the
                # attempted decision is still what gets logged (hook).
                return _end_result(decision["reason"], decision=decision)
            return _handoff_result(target, decision, memory_calls)

        if tool_calls and not memory_calls:
            # The provider invented tools the supervisor does not have;
            # drop them and read the reply as a text decision.
            tool_calls = []

        if memory_calls:
            message = AIMessage(
                content=content or "",
                tool_calls=[_langchain_tool_call(tc) for tc in memory_calls],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

        decision = _decision_from_text(content or "", available)
        if not isinstance(decision, dict):
            decision = {
                "next": "planner" if "planner" in available else available[0],
                "reason": f"Failed to parse supervisor decision: {(content or '')[:100]}",
                "briefing": "",
            }
        next_node = decision.get("next", "END")
        if next_node != "END" and next_node in available:
            return _handoff_result(next_node, decision)
        return _end_result(decision.get("reason") or content or "", decision=decision)


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
    text = _supervisor_system_prompt(
        state.get("original_prompt", "None"),
        state.get("plan", "No plan yet."),
        available,
        tool_mode=True,
    )
    board = blackboard_briefing(state)
    if board:
        text += f"\n\nShared team blackboard (written by all agents in this task):\n{board}"
    return [SystemMessage(content=text), *list(state.get("messages", []))]


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
        if decision is None:
            # Native-tool routing: the briefing is what memory tools
            # returned since the last user message; the reason is the
            # model's own reply text alongside the transfer call.
            briefing_parts = []
            dict_messages = message_dicts(messages)
            last_user = max(
                (i for i, m in enumerate(dict_messages) if m.get("role") == "user"),
                default=-1,
            )
            for msg in dict_messages[last_user + 1:]:
                if msg.get("role") == "tool" and msg.get("content"):
                    briefing_parts.append(msg["content"])
            decision = {
                "next": target,
                "reason": last.content if isinstance(last.content, str) else "",
                "briefing": "\n".join(briefing_parts),
            }
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
