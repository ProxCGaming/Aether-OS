import json
from langchain_core.runnables import RunnableConfig
from aether_engine.langgraph.state import AetherState
from aether_engine.artifacts.manager import save_artifact
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import StreamChunk, ToolCall

async def coder_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Coder node writes, tests, and debugs code.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    provider = config.get("configurable", {}).get("provider")
    
    agent_models = config.get("configurable", {}).get("agent_models", {})
    agent_tools = config.get("configurable", {}).get("agent_tools", {})
    secret_store = config.get("configurable", {}).get("secret_store")
    
    agent_model_override = agent_models.get("coder")
    if agent_model_override and agent_model_override != "inherit" and secret_store:
        try:
            # Format is "provider_name:model_name"
            parts = agent_model_override.split(":", 1)
            if len(parts) == 2:
                p_name, m_name = parts
                from aether_engine.app import _create_provider_instance
                provider = _create_provider_instance(p_name, m_name, secret_store)
        except Exception:
            pass  # fallback to global provider if override fails
            
    if not provider or not hasattr(provider, "call_stream"):
        raise RuntimeError("Valid provider not found in graph config")
    
    registry = config.get("configurable", {}).get("tool_registry")
    all_tools = registry.get_definitions() if registry else []
    
    allowed_tools = agent_tools.get("coder")
    if allowed_tools is not None and isinstance(allowed_tools, list):
        import copy
        used_tools = set()
        for msg in state.get("messages", []):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls"):
                    if isinstance(tc, dict):
                        used_tools.add(tc.get("function", {}).get("name"))
        tools = []
        for t in all_tools:
            t_name = t.get("function", {}).get("name")
            if t_name in allowed_tools:
                tools.append(t)
            elif t_name in used_tools:
                disabled_t = copy.deepcopy(t)
                disabled_t["function"]["description"] = f"DO NOT USE THIS TOOL. IT IS CURRENTLY DISABLED FOR YOUR ROLE."
                tools.append(disabled_t)
    else:
        tools = all_tools
    
    plan_text = state.get("plan", "")
    plan_info = f"\nCurrent Plan & Context:\n{plan_text}\n" if plan_text and plan_text != "No plan currently." else ""
    # Skill Injection
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
    user_config = config.get("configurable", {}).get("user_config")
    attached_skills = GLOBAL_SKILL_MANAGER.get_attached_skills("coder", user_config)
    
    skill_sections = []
    skill_names = set(attached_skills)
    for s_name in skill_names:
        s_content = GLOBAL_SKILL_MANAGER.load_skill_content(s_name)
        if s_content:
            skill_sections.append(f"\n--- SKILL: {s_name} ---\n{s_content}\n--- END SKILL ---")
    skill_text = "\n".join(skill_sections)
    
    system_prompt = f"You are a Coder agent. Write, test, and debug code based on the current plan and context.\n{plan_info}{skill_text}"
    messages = [{"role": "system", "content": system_prompt}] + state.get("messages", [])
    
    if messages and messages[-1].get("role") not in ("user", "tool"):
        messages.append({
            "role": "user",
            "content": "Please proceed with writing, testing, or debugging the code based on the current state."
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
    artifact_path = save_artifact(task_id, "code", "coder_output.txt", content)
    
    return {
        "current_phase": "coding",
        "artifact_paths": {"coder_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
