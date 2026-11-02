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

import logging

from langgraph.graph import END, START, StateGraph
from langgraph_supervisor import create_supervisor

logger = logging.getLogger("aether_engine.langgraph.graph")

from aether_engine.browser.policy import BROWSER_TOOL_NAMES
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


def _is_browser_write(tool_name, registry) -> bool:
    """A non-read-only agent-browser tool (Epic 8, E8-6.1/6.2).

    Resolved from the registry when available (source + risk are the
    authority); the fixed tool-name set is the fallback.
    """
    if not tool_name:
        return False
    if registry is not None:
        try:
            tool = registry.get_tool(tool_name)
            return tool.source == "browser" and tool.risk.value != "read_only"
        except Exception:
            # An unresolvable tool falls back to the name set below;
            # routing must never break on a registry lookup.
            logger.debug("Browser-tool lookup failed in _is_browser_write", exc_info=True)
    return tool_name in BROWSER_TOOL_NAMES and tool_name != "browser_extract"


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
        approval_mode=user_config.approval_mode if user_config else "ask",
    )
    # Epic 8, E8-6.2: browser writes always meet the confirmation
    # gate in the pause node — the per-category browser policy
    # (Settings → Browser Agent Policy), not the generic tool policy
    # or approval mode, decides whether they actually pause. An
    # explicit per-tool Deny still wins: execution denies it there.
    if policy != "deny" and _is_browser_write(pending.get("name"), registry):
        return "pause_for_approval"
    if policy == "ask":
        # E2-09: a stored approval grant skips the pause (registry.execute
        # re-checks the same cache before running, so the two sites agree).
        if registry is not None:
            from aether_engine.tools.approval_cache import get_default_cache

            try:
                tool = registry.get_tool(pending.get("name"))
                if get_default_cache().covering_grant(risk=tool.risk.value, args=pending.get("args")) is not None:
                    return "execute_tool"
            except Exception:
                # A cache failure must degrade to the normal pause, never break routing.
                logger.debug("Approval-cache check failed in tool_router", exc_info=True)
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
