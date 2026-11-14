import json
import logging
import time

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from aether_common.contracts import Event, EventType
from aether_engine.audit import AuditLogger
from aether_engine.event_bus import event_bus
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": (
                "Fetch the stored profile of the user from long-term memory: who they are, "
                "their people and relationships, and their preferences — derived from the "
                "stored facts, each traceable to its fact. Call this when the task is about "
                "the user themselves, their people, or what they prefer."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": (
                "Forget a specific topic from long-term memory when the user asks to "
                "forget it (e.g. 'forget my Berlin trip'). The ENGINE resolves the topic "
                "to concrete facts, past task records and lessons, deletes them itself "
                "(cascading across every memory surface), snapshots them for undo, and "
                "reports exactly what was deleted. Report its result verbatim in "
                "substance: name what was forgotten, and never claim a deletion it did "
                "not report. There is no forget-everything here — a request with no "
                "specific topic belongs in Settings → Memory Management."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "What to forget, as the user named it (e.g. 'Berlin trip', 'my old address', 'Project Falcon')."
                    }
                },
                "required": ["topic"]
            }
        }
    }
]

# Master build S19: the supervisor's desktop-observe tool,
# kept in its own list (not SUPERVISOR_MEMORY_TOOLS) because
# its availability is config-gated in a way the memory tools'
# is not: it is only OFFERED when Computer Use + Observe are
# both enabled (desktop.observe_permitted), and with them off
# the offered tool list and the routing prompt are
# byte-identical to pre-S19. It is a sibling of the memory
# tools in every other respect — it executes inside the
# supervisor under the same deny + allow-list gate pattern
# (_desktop_tool_gate), and its name-level policy class is
# READ_ONLY (tools/policy.py KNOWN_NON_REGISTRY_TOOLS).
SUPERVISOR_SCREEN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_screen_state",
            "description": (
                "Read the user's screen as accessibility TEXT: the foreground "
                "window (title and app), its element tree (names, roles and "
                "values, the focused element marked), and the other visible "
                "windows. Call this only when the task is about what is on "
                "the user's screen or which app/window they are working in. "
                "It reads text the OS exposes — it cannot click, type, or "
                "take screenshots, and nothing is watched continuously."
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

SCREEN_TOOL_NAMES = {t["function"]["name"] for t in SUPERVISOR_SCREEN_TOOLS}


# Epic 4, E4-07: memory-tool results feed the supervisor's context;
# cap them so a large store cannot flood the briefing. Individual
# lines are already short (facts are one-liners, episode outcomes
# truncate at 160 chars) — this is the overall budget.
MEMORY_TOOL_RESULT_MAX_CHARS = 2000


def _forget_result_text(outcome: dict) -> str:
    """Render a forget service outcome for the supervisor: exactly
    what was deleted (the reply must not overclaim beyond this),
    the undo position, and any honest degradation."""
    counts = outcome.get("counts", {})
    total = counts.get("facts", 0) + counts.get("episodes", 0) + counts.get("lessons", 0)
    topic = outcome.get("topic") or ""
    if total == 0:
        return (
            f'Nothing in long-term memory matched "{topic}" — '
            "nothing was deleted. Tell the user exactly that.")
    deleted = outcome.get("deleted", {})
    lines = [f'Forgot "{topic}" — the engine deleted:']
    for fact in deleted.get("facts", []):
        lines.append(f'- fact: {fact.get("text", "")}')
    for episode in deleted.get("episodes", []):
        lines.append(f'- past task record: {episode.get("text", "")}')
    for lesson in deleted.get("lessons", []):
        lines.append(f'- lesson: {lesson.get("text", "")}')
    if outcome.get("undo_supported"):
        lines.append(
            'The deletion is snapshotted: the user can say "undo '
            'forget" in chat to restore it.')
    else:
        lines.append(
            "This deletion cannot be undone from chat with the "
            "active memory backend — do not promise an undo.")
    if outcome.get("truncated_candidates"):
        lines.append(
            f'{outcome["truncated_candidates"]} further matching '
            "item(s) were NOT deleted (the per-request cap); "
            "repeating the request forgets more.")
    return "\n".join(lines)


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
            # S5: the result is assembled by the single context
            # assembler (memory/context_assembler.py) — KG facts and
            # the curated consolidation document scored together
            # (relevance x recency x confidence x importance) under
            # the one context token budget, each line carrying its
            # source id. The tool's own result cap below still
            # applies as an outer safety limit.
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                from aether_engine.memory.context_assembler import (
                    assemble_memory_context,
                )

                query = args.get("query", "")
                assembled = await assemble_memory_context(
                    backend, query, config=user_config, include_episodes=False)
                result_str = assembled["text"] or "No matching facts found in knowledge graph."

        elif tool_name == "search_past_episodes":
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                from aether_engine.memory.context_assembler import (
                    assemble_memory_context,
                )

                query = args.get("query", original_prompt)
                assembled = await assemble_memory_context(
                    backend, query, config=user_config,
                    include_facts=False, include_curated=False)
                result_str = assembled["text"] or "No past episodes found matching query."

        elif tool_name == "forget_memory":
            # S8: conversational forget through the SAME facade
            # service fast chat uses (memory/forget.py) — the
            # engine resolves the topic and deletes, tombstoned
            # for "undo forget"; this branch only formats the
            # outcome. Memory tools execute inside the supervisor
            # under _memory_tool_gate (deny + allow-list); there
            # is no approval-pause seam for the supervisor's own
            # tools, so the tombstone + undo is this tool's safety
            # net, and its name-level policy class is DESTRUCTIVE
            # (tools/policy.py KNOWN_NON_REGISTRY_TOOLS) so every
            # policy surface defaults it to "ask" and can never
            # auto-allow it.
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                topic = str(args.get("topic") or "").strip()
                if not topic:
                    result_str = (
                        "forget_memory needs a topic — ask the user "
                        "what specifically should be forgotten.")
                else:
                    outcome = backend.forget_memory(
                        topic, phrase=original_prompt, config=user_config)
                    result_str = _forget_result_text(outcome)

        elif tool_name == "get_user_profile":
            # S6: the profile VIEW over the KG (memory/profile.py),
            # read through the facade like every memory read. This
            # is a pull, never an injection: the routing prompt's
            # discipline — NEVER call memory tools for
            # self-contained requests — covers this tool too, so
            # the profile enters context only when a task is
            # actually about the user, their people, or their
            # preferences. The result cap below applies as the
            # outer safety limit on top of the profile's own cap.
            backend = backend_or_none(user_config, context="supervisor memory tool")
            if backend is None:
                result_str = "Memory is unavailable: the configured memory backend cannot run here (see engine log)."
            else:
                briefing = backend.profile_briefing()
                result_str = briefing or (
                    "No user profile is stored yet — nothing has been "
                    "learned about the user, their people, or their "
                    "preferences so far.")

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


async def _execute_supervisor_screen_tool(task_id: str, user_config=None) -> str:
    """Execute get_screen_state: one desktop-observe read,
    rendered as the bounded summary. Mirrors
    _execute_supervisor_memory_tool's contract — TOOL_ACTIVITY
    events around the call, an audit record, the result capped
    (the desktop summary's own cap is the primary bound; this
    is the outer safety limit) — and, like the memory tools,
    any failure degrades to an honest string for the model,
    never an exception into the routing step."""
    from aether_engine.desktop import (
        SCREEN_STATE_MAX_CHARS,
        get_desktop_provider,
        summarise_screen_state,
    )

    try:
        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": "get_screen_state",
                    "tool_args": {},
                    "status": "pending",
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))

        state = await get_desktop_provider().screen_state()
        if not state.available:
            result_str = (
                "Desktop observe is unavailable: "
                f"{state.reason or 'no reason was given.'}")
        else:
            result_str = summarise_screen_state(state)
        if len(result_str) > SCREEN_STATE_MAX_CHARS:
            result_str = result_str[:SCREEN_STATE_MAX_CHARS] \
                + "\n...[screen result truncated to budget]"

        _audit_logger.log_event("SUPERVISOR_DESKTOP_TOOL_CALLED", {
            "tool_name": "get_screen_state",
            "result_preview": result_str[:200]
        })

        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": "get_screen_state",
                    "tool_args": {},
                    "status": "completed",
                    "result": result_str,
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))
        return result_str
    except Exception as e:  # noqa: BLE001 — the desktop backend is an external boundary; any failure degrades to an error string for the model.
        logger.warning(f"Supervisor desktop tool get_screen_state failed: {e}")
        if task_id:
            event_bus.publish(task_id, Event(
                type=EventType.TOOL_ACTIVITY,
                payload={
                    "task_id": task_id,
                    "tool_name": "get_screen_state",
                    "tool_args": {},
                    "status": "failed",
                    "result": f"Error: {e}",
                    "node": "supervisor",
                    "timestamp": int(time.time())
                }
            ))
        return f"Error executing get_screen_state: {e}"


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
                    # E5-F02: the run's effective config governs this
                    # construction's fallback policy too.
                    user_config=_cfg.get("user_config"),
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
    # Epic 4, E4-10 (addendum omp #29): the learn loop closes here —
    # lessons recorded at task end in learned.md are injected into
    # the next session's briefing (this prompt), capped by
    # learned_briefing. A missing/broken document means no section,
    # never a routing failure.
    try:
        from aether_engine.memory.learned import learned_briefing

        lessons_section = learned_briefing()
    except Exception:  # noqa: BLE001 — lessons must never break routing
        lessons_section = ""
    # Calendar layer C3 (14 Nov 2026): the itinerary rides beside the
    # lessons under the same contract — best-effort, capped, empty
    # when there is nothing scheduled — so routing always knows
    # today's commitments and clashes without a memory-tool call.
    try:
        from aether_engine.calendar.itinerary import calendar_briefing

        calendar_section = calendar_briefing()
    except Exception:  # noqa: BLE001 — calendar must never break routing
        calendar_section = ""
    return (
        f"{skill_catalog}\n"
        f"{lessons_section + chr(10) if lessons_section else ''}"
        f"{calendar_section + chr(10) if calendar_section else ''}"
        f"You are the Supervisor agent. Your task is to delegate the user's request to one of the specialists: "
        f"{options_str}, or 'END' if the task is complete.\n"
        f"User Request: {original_prompt}\n"
        f"Current plan: {plan}\n\n"
        "You have autonomous access to memory tools:\n"
        "- get_current_chat_history: fetch previous messages in this current open chat session\n"
        "- search_knowledge_graph: fetch long-term user facts, preferences, or project entities\n"
        "- search_past_episodes: fetch past completed tasks across previous sessions\n"
        "- get_user_profile: fetch the stored profile of the user — who they are, their people and relationships, and their preferences\n"
        "- forget_memory: when the user asks to forget a specific topic, delete it from long-term memory (engine-executed, snapshotted for undo); report exactly what it says was deleted — never claim a deletion it did not report, and never use it for a topic-less 'forget everything' (that belongs in Settings → Memory Management)\n\n"
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


def _desktop_tool_gate(tool_name: str, config: RunnableConfig | None) -> str | None:
    """Config + policy gate for the supervisor's desktop tools.

    Desktop tools execute inside the supervisor like the memory
    tools, so the registry pipeline does not see them; the gate
    is enforced here, at call time, in two layers: first the
    Computer Use config (master switch + Observe switch —
    desktop.observe_permitted, the same check that governs
    whether the tool is OFFERED at all, re-checked here so a
    call that arrives anyway is refused honestly), then the
    memory tools' own deny + allow-list gate, unchanged.
    Returns an error string when the call is forbidden, else
    None.
    """
    from aether_engine.desktop import observe_permitted

    configurable = (config or {}).get("configurable", {}) if config else {}
    if not observe_permitted(configurable.get("user_config")):
        return (
            f"Error: Tool '{tool_name}' is not available: Computer Use "
            "observe is off. The user can enable it in Settings → "
            "Computer Use (the master switch and Observe).")
    return _memory_tool_gate(tool_name, config)


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


@tool
async def get_user_profile(*, runtime: ToolRuntime) -> str:
    """Fetch the stored profile of the user from long-term memory: who they are, their people and relationships, and their preferences. Call this when the task is about the user themselves, their people, or what they prefer."""
    gate = _memory_tool_gate("get_user_profile", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_memory_tool(
        "get_user_profile", {},
        state.get("session_id"), state.get("original_prompt", ""), state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


@tool
async def forget_memory(topic: str, *, runtime: ToolRuntime) -> str:
    """Forget a specific topic from long-term memory when the user asks to forget it. The engine resolves the topic to concrete facts, past task records and lessons, deletes them itself (snapshotted for undo), and reports exactly what was deleted — report that, and never claim a deletion it did not report."""
    gate = _memory_tool_gate("forget_memory", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_memory_tool(
        "forget_memory", {"topic": topic},
        state.get("session_id"), state.get("original_prompt", ""), state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


def build_supervisor_memory_tools() -> list:
    """The supervisor's memory tools as LangChain tools (Epic 1).

    These wrap ``_execute_supervisor_memory_tool`` — the shared
    execution path (audit events, TOOL_ACTIVITY events) — for the
    create_supervisor agent's ToolNode.
    """
    return [get_current_chat_history, search_knowledge_graph, search_past_episodes, get_user_profile, forget_memory]


@tool
async def get_screen_state(*, runtime: ToolRuntime) -> str:
    """Read the user's screen as accessibility text: the foreground window and its element tree, plus the other visible windows. Call only when the task is about what is on the user's screen or which app/window they are in. Text only — no clicking, typing, or screenshots."""
    gate = _desktop_tool_gate("get_screen_state", runtime.config)
    if gate:
        return gate
    state = runtime.state
    return await _execute_supervisor_screen_tool(
        state.get("task_id") or "",
        user_config=runtime.config.get("configurable", {}).get("user_config"),
    )


def build_supervisor_desktop_tools() -> list:
    """The supervisor's desktop-observe tools as LangChain
    tools (S19). Bound into the graph beside the memory tools;
    whether the model ever SEES this one is decided per run by
    the adapter's offered-tools gate (Computer Use + Observe
    config), and _desktop_tool_gate re-checks at call time.
    """
    return [get_screen_state]


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
