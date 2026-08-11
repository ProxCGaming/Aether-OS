import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER
from aether_engine.artifacts.manager import save_artifact

# Define native tools (in a real implementation these would map to python functions)
researcher_tools = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "summarize",
            "description": "Summarize text content",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        }
    }
]

async def researcher_node(state: AetherState) -> dict:
    """
    Researcher node gathers info and updates the state.
    """
    model = await GLOBAL_CAPABILITY_ROUTER.select(intent="research")
    
    # In a real implementation we would also pull in MCP tools from registry here.
    # mcp_tools = get_mcp_tools_for_node("researcher")
    # combined_tools = researcher_tools + mcp_tools
    
    system_prompt = "You are a Researcher agent. Gather information and synthesize findings for other specialists."
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        tools=researcher_tools
        # LiteLLM fallback chains and audit logging will handle failure inherently.
    )
    
    content = response.choices[0].message.content or ""
    
    # Save the output as a durable artifact rather than stuffing it directly in state.
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "research", "researcher_output.txt", content)
    
    # Update state: Note how we don't accumulate artifact_paths, we overwrite.
    return {
        "current_phase": "researching",
        "artifact_paths": {"researcher_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
