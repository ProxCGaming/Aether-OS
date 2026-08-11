import operator
from typing import Annotated, Dict, List, Optional, TypedDict, Any

class AetherState(TypedDict):
    original_prompt: str
    
    # LangGraph automatically appends to Annotated lists with operator.add
    messages: Annotated[List[Any], operator.add]
    
    # These fields OVERWRITE the existing value rather than accumulate
    plan: str
    current_phase: str
    artifact_paths: Dict[str, str]
    tool_results_summary: Dict[str, str]
    pending_tool_call: Optional[Dict[str, Any]]
    
    # Structured log entries for delegation decisions
    delegation_log: Annotated[List[Dict[str, Any]], operator.add]
