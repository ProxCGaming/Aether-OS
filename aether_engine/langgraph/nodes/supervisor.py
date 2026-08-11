import json
import time
import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER

async def supervisor_node(state: AetherState) -> dict:
    """
    Supervisor node routes to exactly one specialist or decides it is done.
    """
    model, api_key, api_base = await GLOBAL_CAPABILITY_ROUTER.select(intent="supervision")
    
    # We ask the model to output a simple JSON indicating next step.
    # In a real setup, we'd use function calling or structured output,
    # but LiteLLM can handle standard JSON requests.
    system_prompt = (
        "You are a Supervisor agent. Your task is to delegate the user's request to one of the following specialists: "
        "'researcher', 'planner', 'coder', or 'END' if the task is complete.\n"
        f"User Request: {state.get('original_prompt', 'None')}\n"
        f"Current plan: {state.get('plan', 'No plan yet.')}\n"
        "Output ONLY a JSON object with two keys: 'next' (the name of the specialist or 'END') and 'reason' (why you chose them)."
    )
    
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    if messages and messages[-1].get("role") != "user":
        messages.append({
            "role": "user",
            "content": "Review the progress above. Which specialist should run next? Output JSON with 'next' and 'reason'."
        })
    
    kwargs = {"model": model, "messages": messages, "response_format": {"type": "json_object"}, "api_key": api_key}
    if api_base:
        kwargs["api_base"] = api_base
    response = await litellm.acompletion(**kwargs)
    
    content = response.choices[0].message.content or ""
    
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
