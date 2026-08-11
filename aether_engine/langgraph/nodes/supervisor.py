import json
import time
import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER

async def supervisor_node(state: AetherState) -> dict:
    """
    Supervisor node routes to exactly one specialist or decides it is done.
    """
    model = await GLOBAL_CAPABILITY_ROUTER.select(intent="supervision")
    
    # We ask the model to output a simple JSON indicating next step.
    # In a real setup, we'd use function calling or structured output,
    # but LiteLLM can handle standard JSON requests.
    system_prompt = (
        "You are a Supervisor agent. Your task is to delegate the user's request to one of the following specialists: "
        "'researcher', 'planner', 'coder', or 'END' if the task is complete.\n"
        "Current plan: " + state.get("plan", "No plan yet.") + "\n"
        "Output ONLY a JSON object with two keys: 'next' (the name of the specialist or 'END') and 'reason' (why you chose them)."
    )
    
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        response_format={"type": "json_object"}
    )
    
    content = response.choices[0].message.content
    try:
        decision = json.loads(content)
    except json.JSONDecodeError:
        # Fallback if the model fails to output valid JSON
        decision = {"next": "END", "reason": "Failed to parse supervisor decision."}
        
    next_node = decision.get("next", "END")
    
    log_entry = {
        "timestamp": int(time.time()),
        "prompt_summary": state.get("original_prompt", "")[:50] + "...",
        "decision": decision,
        "task_id": state.get("task_id", "unknown")
    }
    
    return {
        "delegation_log": [log_entry],
        "next": next_node  # This will be picked up by the graph's conditional edge
    }
