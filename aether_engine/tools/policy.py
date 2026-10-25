from typing import Dict
from aether_engine.tools.registry import Tool, ToolRisk

# known supervisor memory / safe built-in tools that might be evaluated without full registry
KNOWN_NON_REGISTRY_TOOLS: Dict[str, ToolRisk] = {
    "get_current_chat_history": ToolRisk.READ_ONLY,
    "search_knowledge_graph": ToolRisk.READ_ONLY,
    "search_past_episodes": ToolRisk.READ_ONLY,
    "get_current_time": ToolRisk.READ_ONLY,
}

def resolve_tool_policy(tool: Tool, user_policies: Dict[str, str]) -> str:
    """
    Resolves the effective policy for a tool based on user settings and its risk.
    Returns: 'allow', 'ask', or 'deny'.
    """
    user_setting = user_policies.get(tool.name)

    # 1. Deny is always honored
    if user_setting == "Deny":
        return "deny"

    # 2. explicit 'Require Approval' is always honored
    if user_setting == "Require Approval":
        return "ask"

    # 3. If risk is not read_only, 'Always Allow' is ignored, and it always defaults to ask
    if tool.risk != ToolRisk.READ_ONLY:
        return "ask"

    # 4. If read_only, and 'Always Allow', we allow it
    if user_setting == "Always Allow":
        return "allow"

    # 5. Default by risk (read_only -> allow)
    # Since we already handled non-read_only above, this is guaranteed to be read_only here.
    return "allow"

def resolve_policy_by_name(tool_name: str, user_policies: Dict[str, str], registry=None) -> str:
    """
    Helper to resolve policy given just a tool name, looking up in registry or fallbacks.
    """
    from aether_engine.tools.registry import ToolNotFoundError, Tool
    
    risk = ToolRisk.EXECUTE
    
    if registry:
        try:
            tool = registry.get_tool(tool_name)
            risk = tool.risk
        except ToolNotFoundError:
            pass
            
    if risk == ToolRisk.EXECUTE and tool_name in KNOWN_NON_REGISTRY_TOOLS:
        risk = KNOWN_NON_REGISTRY_TOOLS[tool_name]
        
    dummy_tool = Tool(name=tool_name, description="", risk=risk)
    return resolve_tool_policy(dummy_tool, user_policies)
