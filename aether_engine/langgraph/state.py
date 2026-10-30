import operator
from typing import Annotated, Any, TypedDict

# Tool-call loop guard (Epic 1, E1-03): this many identical consecutive
# tool calls (same tool, same arguments) trigger a corrective steer in
# execute_tool instead of another execution. Sized from Oh My Pi's
# proven default of 5. Lives here because the node that enforces it
# already imports this module (graph.py would be a circular import).
TOOL_LOOP_GUARD_THRESHOLD = 5

class AetherState(TypedDict):
    original_prompt: str
    
    # LangGraph automatically appends to Annotated lists with operator.add
    messages: Annotated[list[Any], operator.add]
    
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
    
    # Structured log entries for delegation decisions
    delegation_log: Annotated[list[dict[str, Any]], operator.add]
    
    # Session and task identifiers
    task_id: str | None
    session_id: str | None
    context_briefing: str | None
    active_skills: list[str]
