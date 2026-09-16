import json
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.artifacts.manager import save_artifact
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import StreamChunk, ToolCall

async def researcher_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Researcher node gathers info and updates the state.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    provider = config.get("configurable", {}).get("provider")
    if not provider or not isinstance(provider, LiteLLMProvider):
        raise RuntimeError("LiteLLMProvider not found in graph config")
    
    registry = config.get("configurable", {}).get("tool_registry")
    tools = registry.get_definitions() if registry else []
    
    system_prompt = "You are a Researcher agent. Gather information and synthesize findings for other specialists."
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    if messages and messages[-1].get("role") != "user":
        messages.append({
            "role": "user",
            "content": "Please proceed with gathering information and synthesizing findings based on the current state."
        })
    
    content_parts = []
    accumulated_tool_calls = {}
    
    async for chunk in provider.call_stream(messages, tools=tools):
        if chunk.text:
            content_parts.append(chunk.text)
        if chunk.tool_calls:
            for tc in chunk.tool_calls:
                if tc.call_id not in accumulated_tool_calls:
                    accumulated_tool_calls[tc.call_id] = tc
        if chunk.finish_reason:
            break
    
    content = "".join(content_parts)
    
    if accumulated_tool_calls:
        # Take the first tool call
        tc = next(iter(accumulated_tool_calls.values()))
        args = tc.args if isinstance(tc.args, dict) else {}
        
        assistant_msg = {
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(args)}
                }
            ]
        }
        
        return {
            "messages": [assistant_msg],
            "pending_tool_call": {
                "name": tc.name,
                "args": args,
                "call_id": tc.call_id
            }
        }
    
    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "research", "researcher_output.txt", content)
    
    return {
        "current_phase": "researching",
        "artifact_paths": {"researcher_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
