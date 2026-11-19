"""Subagents-as-tools supervisor (P1d): the delegation tools and the
graph builder that replaces ``create_supervisor``.

The supervisor is a plain ``create_react_agent`` over ``AetherState``
— the same agent construction ``create_supervisor`` used internally,
with the same model adapter, routing prompt and post-model hook —
whose delegation tools each invoke one specialist's existing compiled
subgraph and return its result as a tool result. The supervisor then
decides the next step (delegate on, or finish) exactly as the
post-specialist decision worked under the handoff topology.

Why the library goes: ``langgraph-supervisor`` is unmaintained
(0.0.31 is its last release) and LangChain's migration guide directs
users to this pattern. The Step 0 spike (``spike_p1d/`` in the build
worktree; verdict recorded there) proved the two mechanics the
pattern stands on, on the pinned langgraph 1.2.16:

- an approval ``interrupt()`` raised inside a specialist invoked
  from a delegation tool bubbles to the outer graph, and resuming
  the outer graph re-enters the tool and resumes into the same
  specialist — with the specialists compiled with the shared
  checkpointer, exactly as ``_build_specialist_graph`` builds them
  today;
- the specialist receives the full conversation plus the
  ``context_briefing`` the post-model hook wrote immediately before
  the tool ran, and its full message trail lands back in the
  supervisor's history (``full_history`` semantics).

One framework constraint shapes the tool's return: Aether's
``collapse_subgraph_echo`` reducer converts any multi-message
channel write to plain dicts, and the agent's chat-history
validation only recognises ``BaseMessage`` instances — a
``ToolMessage`` batched with the specialist's dict messages would
become a dict and the delegation call would read as unanswered.
The tool therefore returns two ``Command``s: the tool result first
(a single-message write, which stays a ``ToolMessage``), then the
specialist's message delta and merged state channels.
"""

import logging
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolRuntime, create_react_agent
from langgraph.types import Command

from aether_engine.langgraph.fanout import build_parallel_group_tool
from aether_engine.langgraph.nodes.supervisor import (
    build_supervisor_desktop_tools,
    build_supervisor_memory_tools,
)
from aether_engine.langgraph.state import AetherState, message_dicts
from aether_engine.langgraph.supervisor_model import (
    DELEGATION_PREFIX,
    AetherSupervisorModel,
    supervisor_post_model_hook,
    supervisor_routing_prompt,
)

logger = logging.getLogger("aether_engine.langgraph.delegation")

# State channels a specialist subgraph may write, merged back into
# the supervisor's state when a delegation returns. The specialist's
# final state derives from the very state the tool was invoked with,
# so echo-valued writes are no-ops and appended channels merge
# through their reducers exactly as a subgraph node's output did.
# Deliberately NOT merged: ``delegation_count`` (owned by the
# post-model hook — the single increment), ``context_briefing``
# (hook-owned), the run's identity/config channels (task_id,
# session_id, run_config, plan_mode, active_skills, message_id,
# original_prompt) and the managed ``remaining_steps``.
MERGED_CHANNELS = (
    "blackboard",
    "delegation_log",
    "plan",
    "current_phase",
    "artifact_paths",
    "tool_results_summary",
    "pending_tool_call",
    "queued_tool_calls",
    "tool_call_signature",
    "tool_call_repeats",
    "write_tool_used",
    "structured_outputs",
    "subagent_count",
    "context_summary",
    "context_summary_upto",
)


def _final_answer_text(messages: list) -> str:
    """The specialist's final answer in a message delta: the last
    assistant message with content (the same derivation the
    supervisor adapter applies to its post-specialist view)."""
    for msg in reversed(message_dicts(messages)):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


def _make_delegation_tool(name: str, specialist_graph):
    """One delegation tool for one specialist subgraph."""

    @tool(
        f"{DELEGATION_PREFIX}{name}",
        description=(
            f"Delegate a task to the {name} specialist and return its "
            "result. The specialist receives the full conversation and "
            "the briefing; it runs its own tools under the usual "
            "approval gates. 'briefing' carries ONLY facts you "
            "actually retrieved from memory tools for this delegation "
            "(empty string when you retrieved none)."
        ),
    )
    async def _delegate(briefing: str, runtime: ToolRuntime) -> list[Command]:
        state = dict(runtime.state or {})
        # remaining_steps is a managed value: the runtime owns it,
        # and it is not a legal subgraph input.
        sub_input = {k: v for k, v in state.items() if k != "remaining_steps"}
        # An approval interrupt inside the specialist raises
        # GraphInterrupt out of this invoke; it must propagate
        # untouched — the outer graph surfaces it as the run's
        # interrupt, and the resume re-enters this tool and resumes
        # the subgraph into the same specialist (spike-proven).
        final = await specialist_graph.ainvoke(sub_input, config=runtime.config)

        # The specialist's message delta: what its run appended to
        # the history it was invoked with. Compared in dict space —
        # the subgraph stores the input batch dict-converted (the
        # channel reducer's boundary rule), so an object-space
        # prefix check against the agent's live messages would miss
        # and re-append the whole history.
        input_dicts = message_dicts(state.get("messages") or [])
        final_dicts = message_dicts(final.get("messages") or [])
        prefix = 0
        for have, want in zip(final_dicts, input_dicts):
            if have != want:
                break
            prefix += 1
        if prefix < len(input_dicts):
            logger.warning(
                "Delegation to %s: specialist history does not extend "
                "the input history (common prefix %d of %d); merging "
                "the unmatched tail only.",
                name, prefix, len(input_dicts))
        delta = final_dicts[prefix:]
        tool_message = ToolMessage(
            content=_final_answer_text(delta) or f"The {name} specialist finished.",
            name=f"{DELEGATION_PREFIX}{name}",
            tool_call_id=runtime.tool_call_id,
        )
        channels: dict[str, Any] = {}
        for key in MERGED_CHANNELS:
            if key in final:
                channels[key] = final[key]
        return [
            Command(update={"messages": [tool_message]}),
            Command(update={"messages": delta, **channels}),
        ]

    return _delegate


def build_delegation_tools(specialist_graphs: dict) -> list:
    """One delegation tool per specialist, in registry order."""
    return [
        _make_delegation_tool(name, specialist_graph)
        for name, specialist_graph in specialist_graphs.items()
    ]


def create_tools_supervisor_graph(checkpointer, build_specialist_graph,
                                  specialist_nodes):
    """Build the (uncompiled) subagents-as-tools supervisor workflow.

    Mirrors ``create_supervisor``'s construction with the library
    removed: the specialists are compiled by the caller's builder
    (shared checkpointer, unchanged), and the supervisor agent —
    same model adapter, routing prompt, post-model hook and state
    schema as the legacy graph — is the outer graph's only node;
    delegation happens inside its tools, not via sibling nodes.
    """
    specialist_graphs = {
        name: build_specialist_graph(name, node_fn, checkpointer)
        for name, node_fn in specialist_nodes.items()
    }
    supervisor_agent = create_react_agent(
        model=AetherSupervisorModel(),
        tools=(
            build_supervisor_memory_tools()
            + build_supervisor_desktop_tools()
            + build_delegation_tools(specialist_graphs)
            # P2b: the parallel-group tool rides beside the
            # delegation tools (tools topology only — the legacy
            # graph binds no group tool, so no group can be
            # declared there). Its members run as isolated
            # S13-pattern children, never as these shared
            # subgraphs; see langgraph/fanout.py.
            + [build_parallel_group_tool()]
        ),
        prompt=supervisor_routing_prompt,
        state_schema=AetherState,
        post_model_hook=supervisor_post_model_hook,
        name="supervisor",
    )
    builder = StateGraph(AetherState)
    builder.add_node("supervisor", supervisor_agent)
    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", END)
    return builder
