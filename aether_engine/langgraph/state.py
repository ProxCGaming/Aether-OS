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

    # Structured log entries for delegation decisions (echo-collapsed
    # like messages: the supervisor agent returns the full log).
    delegation_log: Annotated[list[dict[str, Any]], collapse_subgraph_echo]

    # Session and task identifiers
    task_id: str | None
    session_id: str | None
    context_briefing: str | None
    active_skills: list[str]

    # Managed by the LangGraph runtime; required in the schema by the
    # prebuilt supervisor agent (langgraph-supervisor / create_react_agent).
    remaining_steps: NotRequired[RemainingSteps]
