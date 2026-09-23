import json
import time
import logging
from typing import Optional, List, Dict, Any
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.memory import episodic, session_store, knowledge_graph
from aether_engine.audit import AuditLogger
from aether_engine.event_bus import event_bus
from aether_common.contracts import Event, EventType

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


async def _execute_supervisor_memory_tool(tool_name: str, args: dict, session_id: Optional[str], original_prompt: str, task_id: str) -> str:
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
            query = args.get("query", "")
            facts = knowledge_graph.search_facts(query, limit=5)
            if not facts:
                facts = knowledge_graph.get_all_facts(limit=5)
            if not facts:
                result_str = "No matching facts found in knowledge graph."
            else:
                lines = [f"- {f.get('entity_name', 'Entity')} ({f.get('entity_type', 'Fact')}): {f.get('fact_text', '')}" for f in facts]
                result_str = "\n".join(lines)

        elif tool_name == "search_past_episodes":
            query = args.get("query", original_prompt)
            episodes = await episodic.query_episodes(query, limit=3)
            if not episodes:
                result_str = "No past episodes found matching query."
            else:
                lines = []
                for ep in episodes:
                    resp_trunc = ep.get('outcome', '')[:160].replace('\n', ' ')
                    lines.append(f"- Past Task: '{ep.get('user_prompt', '')}' -> Outcome: '{resp_trunc}...'")
                result_str = "\n".join(lines)

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
    except Exception as e:
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
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("content"):
            # This is a specialist response — check it's not a tool-call-only message
            if not msg.get("tool_calls"):
                has_specialist_response = True

    if has_specialist_response:
        return True

    return False


async def supervisor_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Supervisor node routes to exactly one specialist or decides it is done.
    Autonomously evaluates whether to call memory tools (chat history, knowledge graph,
    or episodic memory) before deciding delegation and formulating a targeted plan/briefing.
    """
    provider = config.get("configurable", {}).get("provider")
    if not provider or not isinstance(provider, LiteLLMProvider):
        raise RuntimeError("LiteLLMProvider not found in graph config")

    session_id = state.get("session_id") or config.get("configurable", {}).get("session_id")

    # Check if a specialist already responded — if so, end the task
    if _should_end_task(state):
        return {
            "delegation_log": [{
                "timestamp": int(time.time()),
                "prompt_summary": state.get("original_prompt", "")[:50] + "...",
                "decision": {"next": "END", "reason": "Specialist has responded; task complete."},
                "task_id": state.get("task_id", "unknown")
            }],
            "active_specialist": None,
            "next": "END"
        }

    disabled_nodes = config.get("configurable", {}).get("disabled_nodes", set())
    available_nodes = [n for n in ["researcher", "planner", "coder"] if n not in disabled_nodes]
    
    if not available_nodes:
        return {
            "delegation_log": [{
                "timestamp": int(time.time()),
                "prompt_summary": state.get("original_prompt", "")[:50] + "...",
                "decision": {"next": "END", "reason": "All specialists are disabled."},
                "task_id": state.get("task_id", "unknown")
            }],
            "active_specialist": None,
            "next": "END"
        }
        
    options_str = "'" + "', '".join(available_nodes) + "'"
    original_prompt = state.get('original_prompt', 'None')
    
    system_prompt = (
        f"You are the Supervisor agent. Your task is to delegate the user's request to one of the specialists: "
        f"{options_str}, or 'END' if the task is complete.\n"
        f"User Request: {original_prompt}\n"
        f"Current plan: {state.get('plan', 'No plan yet.')}\n\n"
        "You have autonomous access to memory tools:\n"
        "- get_current_chat_history: fetch previous messages in this current open chat session\n"
        "- search_knowledge_graph: fetch long-term user facts, preferences, or project entities\n"
        "- search_past_episodes: fetch past completed tasks across previous sessions\n\n"
        "THINK CAREFULLY:\n"
        "1. If the request is self-contained (e.g. code generation, calculation, generic explanation, basic greeting), "
        "DO NOT call any memory tools. Directly output your JSON routing decision.\n"
        "2. If the request asks for or depends on prior context (e.g. 'what is my name?', 'remember what we discussed?', "
        "'continue the previous work', 'use my preferred language'), CALL the appropriate tool(s) to retrieve the facts.\n"
        "3. Once you have the necessary context, output ONLY a JSON object with three keys:\n"
        f"   - 'next': the specialist name ({options_str}) or 'END'\n"
        "   - 'reason': why you chose them and the operational goal\n"
        "   - 'briefing': concise 1-2 sentence summary of any relevant retrieved facts to guide the specialist (or empty string if none)\n"
        "IMPORTANT: For simple questions or conversational input, delegate to 'planner'. "
        "Only use 'END' if a response has already been provided."
    )

    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])

    if messages and messages[-1].get("role") != "user":
        messages.append({
            "role": "user",
            "content": "Review the progress above. Which specialist should run next? Output JSON with 'next', 'reason', and 'briefing'."
        })

    content_parts = []
    accumulated_tool_calls: Dict[str, ToolCall] = {}
    
    try:
        async for chunk in provider.call_stream(messages, tools=SUPERVISOR_MEMORY_TOOLS):
            if chunk.text:
                content_parts.append(chunk.text)
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    if tc.call_id not in accumulated_tool_calls:
                        accumulated_tool_calls[tc.call_id] = tc
            if chunk.finish_reason:
                break
    except Exception as e:
        logger.warning(f"Supervisor call_stream with tools failed: {e}; falling back to text prompt")
        content_parts = []
        accumulated_tool_calls = {}
        async for chunk in provider.call_stream(messages):
            if chunk.text:
                content_parts.append(chunk.text)

    # If the model called memory tools, execute them and make a second call for the final decision
    if accumulated_tool_calls:
        tool_messages = []
        assistant_tc_list = []
        for tc in accumulated_tool_calls.values():
            args = tc.args if isinstance(tc.args, dict) else {}
            assistant_tc_list.append({
                "id": tc.call_id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(args)}
            })
            result_str = await _execute_supervisor_memory_tool(tc.name, args, session_id, original_prompt, state.get("task_id", ""))
            tool_messages.append({
                "role": "tool",
                "tool_call_id": tc.call_id,
                "name": tc.name,
                "content": result_str
            })

        follow_up_messages = list(messages) + [
            {"role": "assistant", "content": "".join(content_parts) or "", "tool_calls": assistant_tc_list}
        ] + tool_messages + [
            {"role": "user", "content": "Based on the retrieved memory above, provide your final routing decision. Output ONLY JSON with 'next', 'reason', and 'briefing'."}
        ]

        content_parts = []
        try:
            async for chunk in provider.call_stream(follow_up_messages):
                if chunk.text:
                    content_parts.append(chunk.text)
                if chunk.finish_reason:
                    break
        except Exception as e:
            logger.warning(f"Supervisor follow-up call failed: {e}")

    content = "".join(content_parts)

    # Strip markdown code blocks if the model wrapped the JSON
    content_clean = content.strip()
    if content_clean.startswith("```json"):
        content_clean = content_clean[7:]
    if content_clean.startswith("```"):
        content_clean = content_clean[3:]
    if content_clean.endswith("```"):
        content_clean = content_clean[:-3]
    content_clean = content_clean.strip()

    fallback_node = "planner" if "planner" in available_nodes else (available_nodes[0] if available_nodes else "END")
    if not content_clean:
        decision = {"next": fallback_node, "reason": "Empty response from supervisor", "briefing": ""}
    else:
        try:
            decision = json.loads(content_clean)
        except json.JSONDecodeError:
            decision = {"next": fallback_node, "reason": f"Failed to parse supervisor decision: {content_clean[:100]}", "briefing": ""}

    next_node = decision.get("next", "END")
    if next_node not in available_nodes and next_node != "END":
        next_node = "END"

    briefing = (decision.get("briefing") or "").strip()
    reason = decision.get("reason", "")

    # Update plan with targeted briefing context so specialist receives the precise fact without bloated history
    existing_plan = state.get("plan", "")
    if briefing:
        updated_plan = f"Context: {briefing}\nGoal: {reason}"
    else:
        updated_plan = existing_plan if existing_plan and existing_plan != "No plan currently." else f"Goal: {reason}"

    log_entry = {
        "timestamp": int(time.time()),
        "prompt_summary": state.get("original_prompt", "")[:50] + "...",
        "decision": decision,
        "task_id": state.get("task_id", "unknown")
    }

    return {
        "delegation_log": [log_entry],
        "active_specialist": next_node if next_node != "END" else None,
        "next": next_node,
        "plan": updated_plan,
        "context_briefing": briefing or None,
    }
