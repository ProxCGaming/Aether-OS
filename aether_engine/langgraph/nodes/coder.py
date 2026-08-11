import litellm
from aether_engine.langgraph.state import AetherState
from aether_engine.routing.capability_router import GLOBAL_CAPABILITY_ROUTER
from aether_engine.artifacts.manager import save_artifact

coder_tools = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "Execute a shell command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
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
            "name": "test_code",
            "description": "Run unit tests and return results",
            "parameters": {"type": "object", "properties": {"test_command": {"type": "string"}}, "required": ["test_command"]}
        }
    }
]

async def coder_node(state: AetherState) -> dict:
    """
    Coder node writes, tests, and debugs code.
    """
    model = await GLOBAL_CAPABILITY_ROUTER.select(intent="code")
    
    # In a real implementation we would pull dev-oriented MCP tools here.
    
    system_prompt = "You are a Coder agent. Write, test, and debug code based on the current plan."
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        tools=coder_tools
    )
    
    content = response.choices[0].message.content or ""
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "code", "coder_output.txt", content)
    
    return {
        "current_phase": "coding",
        "artifact_paths": {"coder_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
