import json
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.managed import RemainingSteps

# Tool-call loop guard (Epic 1, E1-03): this many identical consecutive
# tool calls (same tool, same arguments) trigger a corrective steer in
# execute_tool instead of another execution. Sized from Oh My Pi's
# proven default of 5. Lives here because the node that enforces it
# already imports this module (graph.py would be a circular import).
TOOL_LOOP_GUARD_THRESHOLD = 5


def to_aether_message(message: Any) -> Any:
    """Convert one LangChain ``BaseMessage`` to Aether's dict shape.

    Aether's message channel is dict-based (``{"role", "content", ...}``),
    but the prebuilt supervisor agent (Epic 1, ``create_supervisor``)
    writes real LangChain messages into the shared channel. Consumers
    that need plain dicts (specialist nodes building provider input, the
    executor translating updates into events) normalise through here.
    Dicts pass through unchanged.
    """
    if isinstance(message, dict) or not isinstance(message, BaseMessage):
        return message
    role = {
        "human": "user",
        "ai": "assistant",
        "system": "system",
        "tool": "tool",
        "function": "tool",
    }.get(message.type, message.type)
    converted: dict[str, Any] = {
        "role": role,
        "content": message.content if isinstance(message.content, str) else str(message.content),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        converted["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": json.dumps(tc.get("args", {})),
                },
            }
            for tc in tool_calls
        ]
    if message.type == "tool":
        converted["tool_call_id"] = getattr(message, "tool_call_id", None)
        if getattr(message, "name", None):
            converted["name"] = message.name
    return converted


def message_dicts(messages: Any) -> list[dict]:
    """Normalise a message list to plain Aether dicts (see above)."""
    return [to_aether_message(m) for m in (messages or [])]


def collapse_subgraph_echo(left: list, right: list) -> list:
    """Reducer for channels shared with compiled subgraphs.

    A subgraph (specialist agent, supervisor agent) receives this
    channel's full value as its input and returns its full final value
    as the node update — including the handoff ``Command`` in
    langgraph-supervisor, which explicitly writes ``{**state, ...}``
    back to the parent. A plain ``operator.add`` would therefore append
    a second copy of the entire history on every delegation. When the
    incoming list starts with the whole current list (the echo shape),
    it IS the new value, not an append.

    LangChain messages riding in a multi-message batch are converted to
    Aether dicts: batches that large only occur at graph boundaries
    (subgraph outputs, handoff commands, run input), never inside the
    supervisor agent, whose own single-message writes must stay real
    ``BaseMessage`` objects for its internal tool routing.
    """
    left = list(left or [])
    right = list(right or [])
    if len(right) > 1:
        right = [to_aether_message(m) for m in right]
    if left and len(right) >= len(left) and right[: len(left)] == left:
        return right
    return left + right


# ── Shared blackboard (Epic 1, E1-15) ────────────────────────────────
# The agents' common working state, carried in the graph state itself
# (owner's choice: LangGraph state, not a parallel store) and persisted
# by the checkpointer across pause/resume/crash. Sections:
#   plan           — str, overwritten by whoever plans last
#   findings       — list[str], append-merged (research results, facts)
#   decisions      — list[str], append-merged (delegation/commitments)
#   open_questions — list[str], overwritten (a question leaves the list
#                    by being answered, so append semantics cannot work)
BLACKBOARD_LIST_CAP = 50
BLACKBOARD_BRIEFING_MAX_CHARS = 1200


def merge_mapping(left: dict | None, right: dict | None) -> dict:
    """Key-wise merge for dict channels written per-agent (E1-10)."""
    return {**(left or {}), **(right or {})}


def merge_blackboard(left: dict | None, right: dict | None) -> dict:
    """Reducer for the blackboard channel (see the section rules above).

    Subgraph outputs echo the full current value back (the same shape
    collapse_subgraph_echo handles for lists): when an incoming list
    starts with the whole current list, it IS the new value, not an
    append — otherwise every delegation would duplicate every entry.
    """
    merged: dict[str, Any] = dict(left or {})
    for key, value in (right or {}).items():
        if key in ("findings", "decisions") and isinstance(value, list):
            current = list(merged.get(key) or [])
            incoming = list(value)
            if current and len(incoming) >= len(current) and incoming[: len(current)] == current:
                merged[key] = incoming[-BLACKBOARD_LIST_CAP:]
            else:
                merged[key] = (current + incoming)[-BLACKBOARD_LIST_CAP:]
        else:
            merged[key] = value
    return merged


def blackboard_briefing(state: dict, *, max_chars: int = BLACKBOARD_BRIEFING_MAX_CHARS) -> str:
    """Render the blackboard as a bounded prompt section.

    The optimisation the owner asked for: agents never receive the raw
    blackboard, only this summary — plan excerpt, the most recent
    findings/decisions, and open questions — hard-capped at max_chars
    regardless of how much the board holds.
    """
    board = (state or {}).get("blackboard") or {}
    parts: list[str] = []
    plan = (board.get("plan") or "").strip()
    if plan:
        parts.append(f"Plan: {plan[:300]}")
    for key, label in (("findings", "Findings"), ("decisions", "Decisions")):
        entries = [str(e).strip() for e in (board.get(key) or []) if str(e).strip()]
        if entries:
            recent = entries[-5:]
            parts.append(f"{label} (latest {len(recent)} of {len(entries)}): " + " | ".join(recent))
    questions = [str(q).strip() for q in (board.get("open_questions") or []) if str(q).strip()]
    if questions:
        parts.append("Open questions: " + " | ".join(questions[-5:]))
    text = "\n".join(parts)
    return text[:max_chars]


class AetherState(TypedDict):
    original_prompt: str

    # Messages accumulate, with subgraph-echo collapse (see the reducer).
    messages: Annotated[list[Any], collapse_subgraph_echo]

    # These fields OVERWRITE the existing value rather than accumulate
    plan: str
    current_phase: str
    artifact_paths: dict[str, str]
    tool_results_summary: dict[str, str]
    pending_tool_call: dict[str, Any] | None
    active_specialist: str | None

    # Tool-call loop guard (Epic 1, E1-03): signature of the most recent
    # tool call and how many times it has repeated consecutively.
    tool_call_signature: str | None
    tool_call_repeats: int

    # Prewalk (Epic 1, E1-13): set by execute_tool once a non-READ_ONLY
    # tool has actually executed; prewalk-enabled specialists plan on
    # the run provider until this flips, then switch to their per-agent
    # (execution) model.
    write_tool_used: bool

    # Structured log entries for delegation decisions (echo-collapsed
    # like messages: the supervisor agent returns the full log).
    delegation_log: Annotated[list[dict[str, Any]], collapse_subgraph_echo]

    # Shared agent blackboard (Epic 1, E1-15): every agent reads and
    # writes it through state updates; see merge_blackboard for the
    # per-section merge rules and blackboard_briefing for how it is
    # injected into prompts (bounded summary, never a raw dump).
    blackboard: Annotated[dict[str, Any], merge_blackboard]

    # Structured outputs (Epic 1, E1-10): agent name -> parsed value
    # validated against the agent's configured output schema (or
    # {"error": ...} after a failed correction retry). Key-merged so
    # specialists never erase each other's entries.
    structured_outputs: Annotated[dict[str, Any], merge_mapping]

    # Plan mode (Epic 1, E1-09): when True, specialists only receive
    # READ_ONLY tools and execute_tool refuses anything riskier. Lives
    # in state (not just config) so it survives the approval pause via
    # the checkpointer with no resume-time threading.
    plan_mode: bool

    # Conversation compaction (Epic 1, E1-06): a summary of the older
    # turns of this thread's history, and how many channel messages it
    # covers. The messages channel itself is NEVER rewritten by
    # compaction — models see summary + tail through
    # compaction.model_facing_messages, and the full history stays in
    # the checkpoint. The marker makes compaction incremental: the
    # next round folds only messages beyond it.
    context_summary: str | None
    context_summary_upto: int

    # Session and task identifiers
    task_id: str | None
    session_id: str | None
    context_briefing: str | None
    active_skills: list[str]

    # Managed by the LangGraph runtime; required in the schema by the
    # prebuilt supervisor agent (langgraph-supervisor / create_react_agent).
    remaining_steps: NotRequired[RemainingSteps]
