import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER
from aether_engine.artifacts.manager import save_artifact

planner_tools = [
    {
        "type": "function",
        "function": {
            "name": "query_state",
            "description": "Read read-only state from other nodes (e.g. read researcher or coder output)",
            "parameters": {"type": "object", "properties": {"artifact_key": {"type": "string"}}, "required": ["artifact_key"]}
        }
    }
]

async def planner_node(state: AetherState) -> dict:
    """
    Planner node breaks goals into structured steps and monitors progress.
    """
    model = await GLOBAL_CAPABILITY_ROUTER.select(intent="reasoning")
    
    system_prompt = "You are a Planner agent. Break goals into structured steps, monitor progress, and adapt the plan. Output your new plan."
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        tools=planner_tools
    )
    
    content = response.choices[0].message.content or ""
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "plan", "planner_output.txt", content)
    
    return {
        "current_phase": "planning",
        "plan": content,  # Overwrites existing plan
        "artifact_paths": {"planner_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
