import json
import time
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import StreamChunk


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
    system_prompt = (
        "You are a Supervisor agent. Your task is to delegate the user's request to one of the following specialists: "
        "'researcher', 'planner', 'coder', or 'END' if the task is complete.\n"
        f"User Request: {state.get('original_prompt', 'None')}\n"
        f"Current plan: {state.get('plan', 'No plan yet.')}\n"
        "IMPORTANT: For simple greetings (hi, hello, hii, hey), questions, or conversational input, "
        "delegate to 'planner' to generate a friendly response. Only use 'END' if the task is truly complete "
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

    try:
        decision = json.loads(content_clean)
    except json.JSONDecodeError:
        # Fallback if the model fails to output valid JSON
        decision = {"next": "END", "reason": f"Failed to parse supervisor decision from: {content[:50]}"}

    next_node = decision.get("next", "END")

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

