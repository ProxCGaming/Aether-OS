import json
import logging
import time

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from aether_common.contracts import Event, EventType
from aether_engine.audit import AuditLogger
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.state import AetherState, message_dicts
from aether_engine.memory import session_store
from aether_engine.memory.facade import backend_or_none
from aether_engine.tools.policy import resolve_policy_by_name

logger = logging.getLogger("aether_engine.langgraph.supervisor")
_audit_logger = AuditLogger()

SUPERVISOR_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_chat_history",
            "description": (
                "Retrieve recent messages from the currently open chat session. "
                "Call this if the user refers to something said earlier in this conversation, "
                "asks for their name, or references earlier discussion in this chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent messages to retrieve (default 6).",
                        "default": 6
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_graph",
            "description": (
                "Search the persistent knowledge graph for stored facts about the user (e.g. name, preferences, identity) "
                "or project entities. Call this when answering questions about user identity, preferences, or established facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query or entity name (e.g. 'user', 'name', 'preference', 'project')."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_past_episodes",
            "description": (
                "Search episodic memory for past completed tasks and sessions. "
                "Call this if the user asks about prior tasks, previous debugging solutions, or work done in past sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords describing the past task to recall."
                    }
                },
                "required": ["query"]
            }
        }
    }
]


# Epic 4, E4-07: memory-tool results feed the supervisor's context;
# cap them so a large store cannot flood the briefing. Individual
# lines are already short (facts are one-liners, episode outcomes
# truncate at 160 chars) — this is the overall budget.
MEMORY_TOOL_RESULT_MAX_CHARS = 2000


async def _execute_supervisor_memory_tool(tool_name: str, args: dict, session_id: str | None, original_prompt: str, task_id: str, user_config=None) -> str:
    try:
        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": tool_name,
                    "tool_args": args,
                    "status": "pending",
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))

        result_str = f"Unknown tool: {tool_name}"
        if tool_name == "get_current_chat_history":
            if not session_id:
                result_str = "No active session ID found."
            else:
                sess = session_store.get_session(session_id)
                if not sess or not sess.get("messages"):
                    result_str = "No prior messages found in this chat session."
                else:
                    msgs = sess["messages"]
                    formatted = []
                    for m in msgs:
                        formatted.append(f"{m['role'].capitalize()}: {m['content']}")
                    if formatted and formatted[-1].strip().lower() == f"user: {original_prompt.strip().lower()}":
                        formatted = formatted[:-1]
                    if not formatted:
                        result_str = "No prior messages found in this chat session."
                    else:
                        limit = int(args.get("limit", 6))
                        result_str = "\n".join(formatted[-limit:])

        elif tool_name == "search_knowledge_graph":
            # Epic 4, E4-01: through the facade (configured backend).
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                query = args.get("query", "")
                facts = backend.search_facts(query, limit=5)
                if not facts:
                    facts = backend.list_facts(limit=5)
                if not facts:
                    result_str = "No matching facts found in knowledge graph."
                else:
                    lines = [f"- {f.get('entity_name', 'Entity')} ({f.get('entity_type', 'Fact')}): {f.get('fact_text', '')}" for f in facts]
                    result_str = "\n".join(lines)

        elif tool_name == "search_past_episodes":
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                query = args.get("query", original_prompt)
                episodes = await backend.search_episodes(query, limit=3)
                if not episodes:
                    result_str = "No past episodes found matching query."
                else:
                    lines = []
                    for ep in episodes:
                        resp_trunc = ep.get('outcome', '')[:160].replace('\n', ' ')
                        lines.append(f"- Past Task: '{ep.get('user_prompt', '')}' -> Outcome: '{resp_trunc}...'")
                    result_str = "\n".join(lines)

        if len(result_str) > MEMORY_TOOL_RESULT_MAX_CHARS:
            result_str = result_str[:MEMORY_TOOL_RESULT_MAX_CHARS] + "\n...[memory result truncated to budget]"

        _audit_logger.log_event("SUPERVISOR_MEMORY_TOOL_CALLED", {
            "tool_name": tool_name,
            "args": args,
            "session_id": session_id,
            "result_preview": result_str[:200]
        })
        
        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": tool_name,
                    "tool_args": args,
                    "status": "completed",
                    "result": result_str,
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))
            
        return result_str
    except Exception as e:  # noqa: BLE001 — memory backends are external boundaries; any failure degrades to an error string for the model, logged above.
        logger.warning(f"Supervisor memory tool {tool_name} failed: {e}")
        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": tool_name,
                    "tool_args": args,
                    "status": "failed",
                    "result": f"Error: {e}",
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))
        return f"Error executing {tool_name}: {e}"


SPECIALIST_NAMES = ["researcher", "planner", "coder"]
MEMORY_TOOL_NAMES = {t["function"]["name"] for t in SUPERVISOR_MEMORY_TOOLS}


def _resolve_supervisor_provider(config: RunnableConfig):
    """Resolve the supervisor's provider from config.

    Applies the ``agent_models["supervisor"]`` override (via
    ``_create_provider_instance``) on top of the graph's default
    provider. Raises RuntimeError when no usable provider is
    configured. Used by the create_supervisor model adapter
    (``supervisor_model.py``).
    """
    provider = config.get("configurable", {}).get("provider")

    agent_models = config.get("configurable", {}).get("agent_models", {})
    secret_store = config.get("configurable", {}).get("secret_store")

    agent_model_override = agent_models.get("supervisor")
    if agent_model_override and agent_model_override != "inherit" and secret_store:
        try:
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
                )
        except Exception as e:  # noqa: BLE001 — an unusable override falls back to the graph provider, as the node always has.
            logger.warning(f"Supervisor agent_models override {agent_model_override!r} unusable, using default provider: {e}")

    if not provider or not hasattr(provider, "call_stream"):
        raise RuntimeError("Valid provider not found in graph config")
    return provider


def _available_specialists(config: RunnableConfig) -> list[str]:
    """Specialists the supervisor may route to (disabled_nodes removed)."""
    disabled_nodes = config.get("configurable", {}).get("disabled_nodes", set())
    return [n for n in SPECIALIST_NAMES if n not in disabled_nodes]


def _decision_from_text(content: str, available_nodes: list[str]) -> dict:
    """Parse a supervisor routing decision out of raw model text.

    Strips markdown fences, then JSON-parses. Empty or unparseable
    output falls back to the planner (or first available specialist)
    with the reason recorded. Used by the create_supervisor model
    adapter (``supervisor_model.py``).
    """
    content_clean = content.strip()
    content_clean = content_clean.removeprefix("```json")
    content_clean = content_clean.removeprefix("```")
    content_clean = content_clean.removesuffix("```")
    content_clean = content_clean.strip()

    fallback_node = "planner" if "planner" in available_nodes else (available_nodes[0] if available_nodes else "END")
    if not content_clean:
        return {"next": fallback_node, "reason": "Empty response from supervisor", "briefing": ""}
    try:
        return json.loads(content_clean)
    except json.JSONDecodeError:
        return {"next": fallback_node, "reason": f"Failed to parse supervisor decision: {content_clean[:100]}", "briefing": ""}


def _supervisor_system_prompt(original_prompt: str, plan: str, available_nodes: list[str], *, tool_mode: bool = False) -> str:
    """Build the supervisor's routing prompt.

    ``tool_mode=False`` is the original prompt format (JSON decision
    output). ``tool_mode=True`` swaps the decision format for
    transfer-tool delegation, used by the create_supervisor routing
    prompt; everything else (memory guidance, anti-hallucination
    rules) is identical.
    """
    options_str = "'" + "', '".join(available_nodes) + "'"
    immediate = "make your routing decision immediately" if tool_mode else "output your JSON routing decision immediately"
    if tool_mode:
        decision_rules = (
            "3. Facts you retrieve from memory tools are shared with the specialist automatically. "
            "Do NOT infer or hallucinate history — only use briefing context you actually retrieved.\n"
            "4. To delegate, call the matching transfer tool for exactly one specialist "
            f"({options_str}); put the operational goal for the specialist in your reply text alongside the call. "
            "To finish the task, reply with plain text and call no tool."
        )
    else:
        decision_rules = (
            "3. The 'briefing' field is ONLY for facts you directly retrieved from memory tools. "
            "Do NOT infer or hallucinate history — if you did not call a memory tool, set briefing to empty string.\n"
            "4. Once you have the necessary context, output ONLY a JSON object with three keys:\n"
            f"   - 'next': the specialist name ({options_str}) or 'END'\n"
            "   - 'reason': a concise description of the operational goal for the specialist\n"
            "   - 'briefing': ONLY facts retrieved from memory tools (empty string if no tool was called)"
        )
    # Epic 7, E7-04: stage-1 skill disclosure — the supervisor sees
    # the summary catalog (never bodies) so it can route with skill
    # awareness; full instructions load only for activated skills.
    try:
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

        skill_catalog = GLOBAL_SKILL_MANAGER.catalogue()
    except Exception:  # noqa: BLE001 — a broken skills dir must never break routing
        skill_catalog = ""
    return (
        f"{skill_catalog}\n"
        f"You are the Supervisor agent. Your task is to delegate the user's request to one of the specialists: "
        f"{options_str}, or 'END' if the task is complete.\n"
        f"User Request: {original_prompt}\n"
        f"Current plan: {plan}\n\n"
        "You have autonomous access to memory tools:\n"
        "- get_current_chat_history: fetch previous messages in this current open chat session\n"
        "- search_knowledge_graph: fetch long-term user facts, preferences, or project entities\n"
        "- search_past_episodes: fetch past completed tasks across previous sessions\n\n"
        "THINK CAREFULLY:\n"
        "1. NEVER call memory tools for self-contained requests. Examples that must be handled WITHOUT any tool call: "
        "current time/date, greetings, basic math, code generation, generic explanations, definitions. "
        f"For these, {immediate}.\n"
        "2. ONLY call memory tools when the user explicitly refers to prior context, e.g. 'what is my name?', "
        "'remember what we discussed?', 'continue the previous work', 'use my preferred style'.\n"
        f"{decision_rules}\n"
        "IMPORTANT: For simple questions or conversational input, delegate to 'planner'. "
        "Only use 'END' if a response has already been provided."
    )


def _memory_tool_gate(tool_name: str, config: RunnableConfig | None) -> str | None:
    """Policy / allow-list gate for supervisor memory tools.

    Memory tools execute inside the supervisor (they are read-only
    lookups, never registry tools), so the registry pipeline does not
    see them; the deny policy and the per-agent allow-list are enforced
    here instead, at call time. Returns an error string when the call
    is forbidden, else None.
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    user_config = configurable.get("user_config")
    if user_config:
        policy = resolve_policy_by_name(tool_name, user_config.tool_policies)
        if policy == "deny":
            return f"Error: Tool '{tool_name}' is blocked by user policy (Deny)."
    agent_tools = configurable.get("agent_tools", {})
    allowed_tools = agent_tools.get("supervisor")
    if isinstance(allowed_tools, list) and tool_name not in allowed_tools:
        return f"Error: Tool '{tool_name}' is currently disabled for your agent role. You are not allowed to execute it."
    return None


@tool
async def get_current_chat_history(limit: int = 6, *, runtime: ToolRuntime) -> str:
    """Retrieve recent messages from the currently open chat session. Call this if the user refers to something said earlier in this conversation, asks for their name, or references earlier discussion in this chat."""
    gate = _memory_tool_gate("get_current_chat_history", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_memory_tool(
        "get_current_chat_history", {"limit": limit},
        state.get("session_id"), state.get("original_prompt", ""), state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


@tool
async def search_knowledge_graph(query: str, *, runtime: ToolRuntime) -> str:
    """Search the persistent knowledge graph for stored facts about the user (e.g. name, preferences, identity) or project entities. Call this when answering questions about user identity, preferences, or established facts."""
    gate = _memory_tool_gate("search_knowledge_graph", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_memory_tool(
        "search_knowledge_graph", {"query": query},
        state.get("session_id"), state.get("original_prompt", ""), state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


@tool
async def search_past_episodes(query: str, *, runtime: ToolRuntime) -> str:
    """Search episodic memory for past completed tasks and sessions. Call this if the user asks about prior tasks, previous debugging solutions, or work done in past sessions."""
    gate = _memory_tool_gate("search_past_episodes", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_memory_tool(
        "search_past_episodes", {"query": query},
        state.get("session_id"), state.get("original_prompt", ""), state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


def build_supervisor_memory_tools() -> list:
    """The supervisor's memory tools as LangChain tools (Epic 1).

    These wrap ``_execute_supervisor_memory_tool`` — the shared
    execution path (audit events, TOOL_ACTIVITY events) — for the
    create_supervisor agent's ToolNode.
    """
    return [get_current_chat_history, search_knowledge_graph, search_past_episodes]


def _should_end_task(state: AetherState) -> bool:
    """Determine if the supervisor should end the task instead of delegating again.

    End conditions (any one is sufficient):
    1. A specialist has already been delegated to AND produced a text response
       (assistant message with content, no pending tool calls).
    2. The supervisor has already delegated more than once to the same specialist
       without any tool calls in between (loop detection).
    """
    delegation_log = state.get("delegation_log", [])
    messages = state.get("messages", [])
    pending_tool = state.get("pending_tool_call")

    # If a tool call is pending, don't end — the tool flow needs to complete
    if pending_tool:
        return False

    # If the supervisor has never delegated yet, don't end
    if not delegation_log:
        return False

    last_delegation = delegation_log[-1]
    last_specialist = last_delegation.get("decision", {}).get("next", "END")

    # If the last delegation was already END, let the supervisor decide again fresh
    if last_specialist == "END":
        return False

    # Check if there are any assistant messages (from a specialist) AFTER the initial user message
    has_specialist_response = False
    for msg in message_dicts(messages):
        # A specialist response is an assistant message with content and
        # no tool calls (not a tool-call-only message).
        if msg.get("role") == "assistant" and msg.get("content") and not msg.get("tool_calls"):
            has_specialist_response = True

    return bool(has_specialist_response)


def repo_map_context(
    config: RunnableConfig | None, prompt: str, context_text: str = ""
) -> str:
    """Epic 8, E8-3.1: the ambient repo map section for a graph task.

    Built from the run's granted workspace roots, ranked toward
    *prompt* (with *context_text* — the conversation so far — as a
    weaker "files in play" bias), inside the user's token budget
    (``repo_map_token_budget``). Returns "" when no workspace is
    granted, the budget disables the map, or the build fails: the
    map is ambient context and must never break routing. Used by
    the create_supervisor routing prompt
    (``supervisor_model.supervisor_routing_prompt``). Graph tasks
    only — fast chat never calls this (grill Q18).
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    roots = configurable.get("workspace_roots") or []
    if not roots:
        return ""
    user_config = configurable.get("user_config")
    budget = getattr(user_config, "repo_map_token_budget", 1000)
    if not isinstance(budget, int):
        budget = 1000
    try:
        from aether_engine.rag.repo_map import build_repo_map

        text = build_repo_map(
            [str(r) for r in roots], prompt,
            token_budget=budget, context_text=context_text,
        )
    except Exception:  # ambient context must never break routing
        logger.debug("Repo map build failed", exc_info=True)
        return ""
    if not text:
        return ""
    return (
        "Repository map of the granted workspace (code signatures only, "
        "ranked for this task — use it to target file reads; it is not "
        f"the files' content):\n{text}"
    )
