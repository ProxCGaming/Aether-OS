import asyncio
import logging
import uuid
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

try:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from aether_engine.langgraph.graph import compile_graph
    from aether_engine.langgraph.state import AetherState
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    RunnableConfig = Any  # type: ignore
    AsyncSqliteSaver = Any  # type: ignore
    compile_graph = None  # type: ignore
    AetherState = Any  # type: ignore
    _LANGGRAPH_AVAILABLE = False
import aiosqlite

from aether_common.contracts import (
    Event,
    EventType,
    TaskState,
    validate_transition,
)
from aether_engine.providers.base import (
    BaseProvider,
    ProviderError,
    RateLimitError,
    TimeoutError,
    NetworkError,
)
from aether_engine.tools.registry import ToolRegistry
from aether_engine.providers.litellm_provider import LiteLLMProvider

logger = logging.getLogger("aether_engine.langgraph.executor")

def _is_retriable_error(exc: Exception) -> bool:
    """Check if an exception is retriable (should trigger fallback)."""
    if isinstance(exc, ProviderError):
        return exc.retriable
    if isinstance(exc, (RateLimitError, TimeoutError, NetworkError)):
        return True
    # Check for common retriable error strings
    err_str = str(exc).lower()
    retriable_indicators = [
        "503", "502", "504", "500",
        "serviceunavailable", "service unavailable",
        "rate limit", "rate_limit", "429",
        "timeout", "timed out",
        "connection", "connecterror", "remotedisconnected",
        "network", "unreachable",
    ]
    return any(indicator in err_str for indicator in retriable_indicators)

async def run_langgraph_task(
    prompt: str,
    provider: BaseProvider,
    tools: Optional[ToolRegistry] = None,
    task_id: Optional[str] = None,
    request_id: Optional[str] = None,
    max_turns: int = 25,
    approval_handler: Optional[Any] = None,
    workspace_roots: Optional[List[str]] = None,
) -> AsyncGenerator[Event, None]:
    """Execute a task prompt using the LangGraph multi-agent workflow."""
    task_id = task_id or str(uuid.uuid4())
    state_enum = TaskState.PENDING

    # Transition PENDING -> RUNNING
    validate_transition(state_enum, TaskState.RUNNING)
    state_enum = TaskState.RUNNING

    yield Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={"task_id": task_id, "prompt": prompt, "state": state_enum.value},
    )

    # Persistent checkpointer: disk-backed SQLite so paused tasks survive Engine restarts (ADR 0011 §4).
    db_path = Path.home() / ".aether" / "checkpoints.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        
        graph = compile_graph(checkpointer=saver)
        
        # We use request_id as the thread_id so approvals can map back
        thread_id = request_id or str(uuid.uuid4())
        config = {
            "configurable": {
                "thread_id": thread_id,
                "tool_registry": tools,
                "approval_handler": approval_handler,
                "request_id": request_id,
                "workspace_roots": workspace_roots or [],
                "task_class": "standard",
                "provider": provider,
            }
        }
        
        initial_state = {
            "original_prompt": prompt,
            "messages": [{"role": "user", "content": prompt}],
            "plan": "No plan currently.",
            "current_phase": "initializing",
            "artifact_paths": {},
            "tool_results_summary": {},
            "pending_tool_call": None,
            "active_specialist": None,
            "delegation_log": [],
            "task_id": task_id
        }

        start_time = time.time()
        full_response_parts: list[str] = []

        try:
            async for event in graph.astream(initial_state, config=config, stream_mode="updates"):
                # 'event' is a dict containing the node name and its state update
                # e.g., {"researcher": {"messages": [...]}}
                for node_name, state_update in event.items():
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "supervisor" and "delegation_log" in state_update:
                        log_entry = state_update["delegation_log"][-1]
                        decision = log_entry.get("decision", {})
                        next_step = decision.get("next", "UNKNOWN")
                        reason = decision.get("reason", "")
                        yield Event(
                            type=EventType.TASK_PROGRESS,
                            request_id=request_id,
                            payload={
                                "task_id": task_id,
                                "text_delta": f"\n\n[Supervisor] -> Delegating to {next_step.upper()}\nReason: {reason}\n\n",
                                "full_text": "",
                                "turn": 0,
                                "node": node_name,
                            },
                        )
                    elif "messages" in state_update and isinstance(state_update["messages"], list):
                        for msg in state_update["messages"]:
                            if msg.get("role") == "assistant" and msg.get("content"):
                                full_response_parts.append(msg["content"])
                                yield Event(
                                    type=EventType.TASK_PROGRESS,
                                    request_id=request_id,
                                    payload={
                                        "task_id": task_id,
                                        "text_delta": msg["content"],
                                        "full_text": msg["content"],
                                        "turn": 0,
                                        "node": node_name,
                                    },
                                )
                            elif msg.get("role") == "tool":
                                yield Event(
                                    type=EventType.TASK_PROGRESS,
                                    request_id=request_id,
                                    payload={
                                        "task_id": task_id,
                                        "text_delta": f"\n\n[Tool Executed: {msg.get('name')}]\n",
                                        "full_text": f"\n\n[Tool Executed: {msg.get('name')}]\n",
                                        "turn": 0,
                                        "node": node_name,
                                    },
                                )
                    
                    # Check if it was executing a tool
                    if node_name == "execute_tool":
                        current_state_obj = await graph.aget_state(config)
                        current_state = current_state_obj.values
                        messages = current_state.get("messages", [])
                        if messages and messages[-1].get("role") == "tool":
                            pass # Handled above

            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            full_response = "\n".join(full_response_parts).strip()
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=request_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.SUCCEEDED.value,
                    "latency_ms": elapsed_ms,
                    "response": full_response or "(task completed)",
                },
            )
        except Exception as e:
            logger.exception("LangGraph task failed")
            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            retriable = _is_retriable_error(e)
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.FAILED.value,
                    "error": str(e),
                    "retriable": retriable,
                    "latency_ms": elapsed_ms,
                },
            )
