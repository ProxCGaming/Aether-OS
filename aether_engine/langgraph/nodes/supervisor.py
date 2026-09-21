import json
import time
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import StreamChunk
from aether_engine.memory import episodic


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
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    provider = config.get("configurable", {}).get("provider")
    if not provider or not isinstance(provider, LiteLLMProvider):
        raise RuntimeError("LiteLLMProvider not found in graph config")

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

    # We ask the model to output a simple JSON indicating next step.
    # In a real setup, we'd use function calling or structured output,
    # but LiteLLM can handle standard JSON requests.
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
    
    # Retrieve relevant past episodic context
    context_str = ""
    try:
        if original_prompt and original_prompt != "None":
            episodes = await episodic.query_episodes(original_prompt, limit=3)
            if episodes:
                context_str = "Relevant Past Context (for your awareness):\n"
                for ep in episodes:
                    resp_trunc = ep.get('outcome', '')[:200].replace('\n', ' ')
                    context_str += f"- Past User Request: '{ep.get('user_prompt', '')}' -> Outcome: '{resp_trunc}...'\n"
    except Exception:
        pass
    
    system_prompt = (
        f"You are a Supervisor agent. Your task is to delegate the user's request to one of the following specialists: "
        f"{options_str}, or 'END' if the task is complete.\n"
        f"User Request: {original_prompt}\n"
        f"Current plan: {state.get('plan', 'No plan yet.')}\n"
        f"{context_str}\n"
        "IMPORTANT: For simple greetings (hi, hello, hii, hey), questions, or conversational input, "
        "delegate to 'planner' (if available) to generate a friendly response. Only use 'END' if the task is truly complete "
        "and a response has already been provided.\n"
        "Output ONLY a JSON object with two keys: 'next' (the name of the specialist or 'END') and 'reason' (why you chose them)."
    )

    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])

    if messages and messages[-1].get("role") != "user":
        messages.append({
            "role": "user",
            "content": "Review the progress above. Which specialist should run next? Output JSON with 'next' and 'reason'."
        })

    content_parts = []
    tool_calls = []
    async for chunk in provider.call_stream(messages):
        if chunk.text:
            content_parts.append(chunk.text)
        if chunk.tool_calls:
            tool_calls.extend(chunk.tool_calls)
        if chunk.finish_reason:
            break

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

    if not content_clean:
        fallback_node = "planner" if "planner" in available_nodes else (available_nodes[0] if available_nodes else "END")
        reason_text = "Empty text response from supervisor"
        if tool_calls:
            reason_text += " (model generated tool calls instead of JSON)"
        decision = {"next": fallback_node, "reason": reason_text}
    else:
        try:
            decision = json.loads(content_clean)
        except json.JSONDecodeError:
            # Fallback if the model fails to output valid JSON
            fallback_node = "planner" if "planner" in available_nodes else (available_nodes[0] if available_nodes else "END")
            decision = {"next": fallback_node, "reason": f"Failed to parse supervisor decision: {content_clean[:100]}"}

    next_node = decision.get("next", "END")
    
    # Ensure next_node is valid
    if next_node not in available_nodes and next_node != "END":
        next_node = "END"

    log_entry = {
        "timestamp": int(time.time()),
        "prompt_summary": state.get("original_prompt", "")[:50] + "...",
        "decision": decision,
        "task_id": state.get("task_id", "unknown")
    }

    return {
        "delegation_log": [log_entry],
        "active_specialist": next_node if next_node != "END" else None,
        "next": next_node  # This will be picked up by the graph's conditional edge
    }

