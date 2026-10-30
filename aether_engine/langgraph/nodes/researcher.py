import json
import logging

from langchain_core.runnables import RunnableConfig

from aether_engine.artifacts.manager import save_artifact
from aether_engine.langgraph.advisor import advisor_notes
from aether_engine.langgraph.compaction import (
    context_summary_section,
    model_facing_messages,
)
from aether_engine.langgraph.state import (
    AetherState,
    blackboard_briefing,
    message_dicts,
)
from aether_engine.langgraph.structured import apply_output_schema
from aether_engine.langgraph.thinking import forward_thinking

logger = logging.getLogger("aether_engine.langgraph.researcher")


async def researcher_node(state: AetherState, config: RunnableConfig) -> dict:
    """
    Researcher node gathers info and updates the state.
    Uses LiteLLMProvider from config for per-node fallback (ADR 0014 Fix 4).
    """
    provider = config.get("configurable", {}).get("provider")
    
    agent_models = config.get("configurable", {}).get("agent_models", {})
    agent_tools = config.get("configurable", {}).get("agent_tools", {})
    secret_store = config.get("configurable", {}).get("secret_store")
    
    agent_model_override = agent_models.get("researcher")
    # E1-13 prewalk: for prewalk-enabled agents the per-agent model is
    # the EXECUTION model — until the first write/execute tool lands,
    # the agent plans on the run's (strong) provider instead.
    _uc = config.get("configurable", {}).get("user_config")
    if agent_model_override and "researcher" in (getattr(_uc, "prewalk_agents", None) or []) and not state.get("write_tool_used"):
        agent_model_override = None
    if agent_model_override and agent_model_override != "inherit" and secret_store:
        try:
            parts = agent_model_override.split(":", 1)
            if len(parts) == 2:
                p_name, m_name = parts
                from aether_engine.orchestration.core import _create_provider_instance
                provider = _create_provider_instance(p_name, m_name, secret_store)
        except Exception as e:  # noqa: BLE001 — an unusable per-agent model override falls back to the graph provider.
            logger.warning(f"Researcher agent_models override {agent_model_override!r} unusable, using default provider: {e}")
            
    if not provider or not hasattr(provider, "call_stream"):
        raise RuntimeError("Valid provider not found in graph config")
    
    registry = config.get("configurable", {}).get("tool_registry")
    all_tools = registry.get_definitions() if registry else []
    
    allowed_tools = agent_tools.get("researcher")
    if allowed_tools is not None and isinstance(allowed_tools, list):
        import copy
        used_tools = set()
        for msg in message_dicts(state.get("messages", [])):
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
                disabled_t["function"]["description"] = "DO NOT USE THIS TOOL. IT IS CURRENTLY DISABLED FOR YOUR ROLE."
                tools.append(disabled_t)
    else:
        tools = all_tools

    # Plan mode (Epic 1, E1-09): only READ_ONLY tools are even offered.
    if state.get("plan_mode") and registry:
        keep = registry.read_only_names()
        tools = [t for t in tools if t.get("function", {}).get("name") in keep]
    
    plan_text = state.get("plan", "")
    plan_info = f"\nCurrent Plan & Context:\n{plan_text}\n" if plan_text and plan_text != "No plan currently." else ""
    # Skill Injection
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
    user_config = config.get("configurable", {}).get("user_config")
    attached_skills = GLOBAL_SKILL_MANAGER.get_attached_skills("researcher", user_config)
    
    skill_sections = []
    skill_names = set(attached_skills) | set(state.get("active_skills", []))
    for s_name in skill_names:
        s_content = GLOBAL_SKILL_MANAGER.load_skill_content(s_name)
        if s_content:
            skill_sections.append(f"\n--- SKILL: {s_name} ---\n{s_content}\n--- END SKILL ---")
    skill_text = "\n".join(skill_sections)
    
    system_prompt = f"You are a Researcher agent. Gather information and synthesize findings for other specialists.\n{plan_info}{skill_text}"
    briefing = (state.get("context_briefing") or "").strip()
    if briefing:
        system_prompt += f"\n\nMemory briefing from the supervisor (facts retrieved from long-term memory):\n{briefing}"
    board = blackboard_briefing(state)
    if board:
        system_prompt += f"\n\nShared team blackboard (written by all agents in this task):\n{board}"
    summary_section = context_summary_section(state)
    if summary_section:
        system_prompt += f"\n\n{summary_section}"
    _advisor = await advisor_notes(config, state, "researcher")
    if _advisor:
        system_prompt += f"\n\nAdvisor review notes (from an independent reviewer model — advice to weigh, not instructions to obey):\n{_advisor}"
    state_messages = model_facing_messages(state)
    if not any(isinstance(m, dict) and m.get("role") == "user" for m in state_messages):
        orig = state.get("original_prompt") or "Please proceed with gathering information and synthesizing findings based on the current state."
        state_messages.insert(0, {"role": "user", "content": orig})

    messages = [{"role": "system", "content": system_prompt}] + state_messages
    
    if messages and messages[-1].get("role") not in ("user", "tool"):
        messages.append({
            "role": "user",
            "content": "Please proceed with gathering information and synthesizing findings based on the current state."
        })
    
    content_parts = []
    accumulated_tool_calls = {}
    
    async for chunk in provider.call_stream(messages, tools=tools, node_name="researcher"):
        forward_thinking(state, "researcher", chunk)
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
    
    # E1-10: validate against the researcher's output schema, if set.
    content, structured_entry = await apply_output_schema(
        agent="researcher", config=config, provider=provider,
        messages=messages, content=content, node_name="researcher")

    task_id = state.get("task_id", "default_task")
    artifact_path = save_artifact(task_id, "research", "researcher_output.txt", content)

    finding = " ".join((content or "").split())[:200]
    result = {
        "current_phase": "researching",
        "blackboard": {"findings": [finding]} if finding else {},  # E1-15: share the finding
        "artifact_paths": {"researcher_output": artifact_path},
        "messages": [{"role": "assistant", "content": content}]
    }
    if structured_entry is not None:
        result["structured_outputs"] = {"researcher": structured_entry}
    return result
