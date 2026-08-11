import json
import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER
from aether_engine.artifacts.manager import save_artifact
from langchain_core.runnables import RunnableConfig

async def planner_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Planner node breaks goals into structured steps and monitors progress.
    """
    model, api_key, api_base = await GLOBAL_CAPABILITY_ROUTER.select(intent="reasoning")
    
    registry = config.get("configurable", {}).get("tool_registry")
    tools = registry.get_definitions() if registry else []
    
    system_prompt = "You are a Planner agent. Break goals into structured steps, monitor progress, and adapt the plan. Output your new plan."
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    if messages and messages[-1].get("role") != "user":
        messages.append({
            "role": "user",
            "content": "Please proceed with breaking goals into structured steps and updating the plan based on the current state."
        })
    
    kwargs = {"model": model, "messages": messages, "api_key": api_key}
    if api_base:
        kwargs["api_base"] = api_base
    if tools:
        kwargs["tools"] = tools
    response = await litellm.acompletion(**kwargs)
    
    msg = response.choices[0].message
    
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        tc = msg.tool_calls[0]
        func = tc.function
        args = json.loads(func.arguments) if isinstance(func.arguments, str) else func.arguments
        
        assistant_msg = {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": func.name, "arguments": func.arguments}
                }
            ]
        }
        
        return {
            "messages": [assistant_msg],
            "pending_tool_call": {
                "name": func.name,
                "args": args,
                "call_id": tc.id
            }
        }
    
    content = msg.content or ""
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "plan", "planner_output.txt", content)
    
    return {
        "current_phase": "planning",
        "plan": content,  # Overwrites existing plan
        "artifact_paths": {"planner_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
