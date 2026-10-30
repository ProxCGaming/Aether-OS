"""Top-level graph assembly (Epic 1: prebuilt supervisor + subgraphs).

The top-level graph is built by LangGraph's ``create_supervisor()``:
the supervisor agent routes between three compiled specialist
subgraphs. Each subgraph wraps the specialist's existing node function
with the shared central tool pipeline — ``pause_for_approval`` (the
Phase 9 approval interrupt) and ``execute_tool`` (``ToolRegistry
.execute()``) — so a specialist's tool calls traverse the same
policy / pre-flight / sandbox path they always have, and the approval
interrupt still fires (and resumes) from inside the subgraph.

The supervisor's routing model is ``AetherSupervisorModel``, an
adapter over Aether's provider layer (per-agent model overrides
included); the supervisor's memory tools run through the same audited
execution path as before. See ``supervisor_model.py``.
"""

from langgraph.graph import END, START, StateGraph
from langgraph_supervisor import create_supervisor

from aether_engine.langgraph.nodes.coder import coder_node
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.langgraph.nodes.pause import dummy_pause_node
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.nodes.researcher import researcher_node
from aether_engine.langgraph.nodes.supervisor import build_supervisor_memory_tools
from aether_engine.langgraph.state import AetherState
from aether_engine.langgraph.supervisor_model import (
    AetherSupervisorModel,
    supervisor_post_model_hook,
    supervisor_routing_prompt,
)
from aether_engine.tools.policy import resolve_policy_by_name

# Recursion ceiling for a single run, shared by the executor's run and
# resume paths: runaway supervisor<->specialist delegation loops and
# tool-call loops terminate with a clear failure instead of spinning
# forever. Legitimate multi-step tasks use far fewer steps than this.
GRAPH_RECURSION_LIMIT = 25

SPECIALIST_NODES = {
    "researcher": researcher_node,
    "planner": planner_node,
    "coder": coder_node,
}


def tool_router(state: AetherState, config=None) -> str:
    """Route a specialist's pending tool call (ADR 0021, Layer 2).

    No pending call means the specialist is done (``"supervisor"`` —
    the subgraph maps that to its END, returning control to the
    supervisor). A pending call whose resolved policy is ``"ask"``
    goes through the approval pause; anything else goes straight to
    central execution (denials are enforced inside the pipeline).
    """
    pending = state.get("pending_tool_call")
    if not pending:
        return "supervisor"

    configurable = (config or {}).get("configurable", {})
    registry = configurable.get("tool_registry")
    user_config = configurable.get("user_config")
    policies = user_config.tool_policies if user_config else {}
    policy = resolve_policy_by_name(
        pending.get("name"),
        policies,
        registry,
        args=pending.get("args"),
        path_rules=user_config.path_rules if user_config else None,
        shell_extras=(
            {"deny": user_config.extra_shell_deny_patterns, "ask": user_config.extra_shell_ask_patterns}
            if user_config else None
        ),
    )
    if policy == "ask":
        return "pause_for_approval"
    return "execute_tool"


def _build_specialist_graph(name: str, node_fn, checkpointer=None):
    """Compile one specialist subgraph around the central tool pipeline."""
    builder = StateGraph(AetherState)
    builder.add_node(name, node_fn)
    builder.add_node("execute_tool", execute_tool_node)
    builder.add_node("pause_for_approval", dummy_pause_node)

    builder.add_edge(START, name)
    builder.add_conditional_edges(
        name,
        tool_router,
        {
            "pause_for_approval": "pause_for_approval",
            "execute_tool": "execute_tool",
            "supervisor": END,
        },
    )
    builder.add_edge("pause_for_approval", "execute_tool")
    builder.add_edge("execute_tool", name)
    return builder.compile(checkpointer=checkpointer, name=name)


def create_graph(checkpointer=None):
    """Build the (uncompiled) supervisor workflow over the specialists."""
    specialists = [
        _build_specialist_graph(name, node_fn, checkpointer)
        for name, node_fn in SPECIALIST_NODES.items()
    ]
    return create_supervisor(
        specialists,
        model=AetherSupervisorModel(),
        tools=build_supervisor_memory_tools(),
        prompt=supervisor_routing_prompt,
        state_schema=AetherState,
        post_model_hook=supervisor_post_model_hook,
        output_mode="full_history",
        add_handoff_messages=False,
        add_handoff_back_messages=False,
    )


def compile_graph(checkpointer=None):
    """Compile the full graph — the executor's entry point (unchanged)."""
    return create_graph(checkpointer=checkpointer).compile(checkpointer=checkpointer)
