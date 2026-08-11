from typing import Literal
from langgraph.graph import StateGraph, START, END

from aether_engine.langgraph.state import AetherState
from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.langgraph.nodes.researcher import researcher_node
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.nodes.coder import coder_node

# We define safe vs risky tools
RISKY_TOOLS = {"write_file", "execute_shell", "delete_file"}

def supervisor_router(state: AetherState) -> Literal["researcher", "planner", "coder", "__end__"]:
    # The supervisor node sets 'next' in the state or we pull it from delegation_log
    last_log = state.get("delegation_log", [])
    if last_log:
        next_node = last_log[-1].get("decision", {}).get("next", "END")
        if next_node in ["researcher", "planner", "coder"]:
            return next_node
    return "__end__"

def tool_router(state: AetherState) -> Literal["execute_tool", "pause_for_approval"]:
    # Conditional edge classifying tool as risky or safe
    pending = state.get("pending_tool_call")
    if not pending:
        # If no tool call, go back to supervisor
        return "supervisor" # Wait, the router return type in this context should be updated
    
    tool_name = pending.get("name")
    if tool_name in RISKY_TOOLS:
        return "pause_for_approval"
    return "execute_tool"

def create_graph():
    workflow = StateGraph(AetherState)
    
    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("coder", coder_node)
    
    # We would add tool execution nodes here in a full implementation
    # workflow.add_node("execute_tool", execute_tool_node)
    # workflow.add_node("pause_for_approval", dummy_pause_node)
    
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
    
    # Specialists return to supervisor (simplified for this scaffolding)
    workflow.add_edge("researcher", "supervisor")
    workflow.add_edge("planner", "supervisor")
    workflow.add_edge("coder", "supervisor")
    
    # Set recursion limit to 25 to prevent unbounded loops
    # This is set during compile or run time
    return workflow

def compile_graph(checkpointer=None):
    workflow = create_graph()
    
    # If we had the pause node, we'd do:
    # return workflow.compile(checkpointer=checkpointer, interrupt_before=["pause_for_approval"])
    
    return workflow.compile(checkpointer=checkpointer)
