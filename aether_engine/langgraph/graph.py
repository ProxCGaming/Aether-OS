from typing import Literal, Any, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
try:
    from langgraph.config import get_config
except ImportError:
    get_config = None  # older langgraph versions

from aether_engine.langgraph.state import AetherState
from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.langgraph.nodes.researcher import researcher_node
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.nodes.coder import coder_node
from aether_engine.tools.policy import resolve_policy_by_name

def supervisor_router(state: AetherState) -> Literal["researcher", "planner", "coder", "__end__"]:
    # The supervisor node sets 'next' in the state or we pull it from delegation_log
    last_log = state.get("delegation_log", [])
    if last_log:
        next_node = last_log[-1].get("decision", {}).get("next", "END")
        if next_node in ["researcher", "planner", "coder"]:
            return next_node
    return "__end__"

def tool_router(state: AetherState, config: RunnableConfig | None = None) -> Literal["execute_tool", "pause_for_approval", "supervisor"]:
    # Conditional edge using policy to determine if approval is needed
    pending = state.get("pending_tool_call")
    if not pending:
        # If no tool call, go back to supervisor
        return "supervisor"
    
    tool_name = pending.get("name")
    
    configurable = {}
    if config and isinstance(config, dict):
        configurable = config.get("configurable", {})
    if not configurable and get_config is not None:
        try:
            configurable = get_config().get("configurable", {})
        except Exception:
            pass
            
    user_config = configurable.get("user_config")
    registry = configurable.get("tool_registry")
    user_policies = user_config.tool_policies if user_config else {}
    
    policy = resolve_policy_by_name(tool_name, user_policies, registry)
    if policy == "ask":
        return "pause_for_approval"
    return "execute_tool"

def after_tool_router(state: AetherState) -> str:
    active = state.get("active_specialist")
    if active in ["researcher", "planner", "coder"]:
        return active
    return "supervisor"

def create_graph():
    workflow = StateGraph(AetherState)
    
    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("coder", coder_node)
    
    # Add tool execution nodes
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
    from aether_engine.langgraph.nodes.pause import dummy_pause_node
    workflow.add_node("execute_tool", execute_tool_node)
    workflow.add_node("pause_for_approval", dummy_pause_node)
    
    # Add edges
    workflow.add_edge(START, "supervisor")
    
    # Supervisor routes to one of the specialists or END
    workflow.add_conditional_edges(
        "supervisor",
        supervisor_router,
        {
            "researcher": "researcher",
            "planner": "planner",
            "coder": "coder",
            "__end__": END
        }
    )
    
    # Specialists check if they need to call a tool or return to supervisor
    workflow.add_conditional_edges("researcher", tool_router)
    workflow.add_conditional_edges("planner", tool_router)
    workflow.add_conditional_edges("coder", tool_router)

    # Tool execution returns to the active specialist
    workflow.add_conditional_edges("execute_tool", after_tool_router)
    
    # Pause node goes to execute tool after un-paused
    workflow.add_edge("pause_for_approval", "execute_tool")
    
    # Set recursion limit to 25 to prevent unbounded loops
    # This is set during compile or run time
    return workflow

def compile_graph(checkpointer=None):
    workflow = create_graph()
    return workflow.compile(checkpointer=checkpointer)
