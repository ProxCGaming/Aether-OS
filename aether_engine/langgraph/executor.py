import asyncio
import logging
import uuid
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

try:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command
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
from aether_engine.memory import episodic
from aether_engine.memory import knowledge_graph
from aether_engine.event_bus import event_bus
import json

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
    disabled_nodes: Optional[set] = None,
    session_id: Optional[str] = None,
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
                "disabled_nodes": disabled_nodes or set(),
                "session_id": session_id,
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
            "task_id": task_id,
            "session_id": session_id,
            "context_briefing": None,
        }

        start_time = time.time()
        full_response_parts: list[str] = []
        interrupted = False
        thinking_log = []
        accumulated_thoughts = []

        def record_thought(ev: Event):
            p = ev.payload
            if ev.type == EventType.TOOL_ACTIVITY:
                st = p.get("status")
                t_name = p.get("tool_name")
                if st != "pending":
                    for t in reversed(accumulated_thoughts):
                        if t.get("type") == "TOOL_ACTIVITY" and t.get("payload", {}).get("tool_name") == t_name and t.get("payload", {}).get("status") == "pending":
                            prev_args = t.get("payload", {}).get("tool_args", {})
                            new_args = p.get("tool_args", {})
                            merged_args = new_args if (new_args and len(new_args) > 0) else prev_args
                            t["payload"] = {**p, "tool_args": merged_args}
                            return
                accumulated_thoughts.append({"type": "TOOL_ACTIVITY", "payload": p, "node": p.get("node")})
            elif ev.type == EventType.NODE_ACTIVITY:
                accumulated_thoughts.append({"type": "NODE_ACTIVITY", "payload": p, "node": p.get("from_node")})
            elif ev.type == EventType.TASK_PROGRESS:
                n = p.get("node")
                d = p.get("text_delta")
                if n and n not in ("assistant", "agent") and d:
                    accumulated_thoughts.append({"node": n, "text": d})

        def emit_event(ev: Event) -> Event:
            record_thought(ev)
            return ev

        event_queue = asyncio.Queue()
        event_bus.subscribe(task_id, event_queue)

        async def _graph_runner():
            try:
                async for evt in graph.astream(initial_state, config=config, stream_mode="updates"):
                    await event_queue.put(("graph", evt))
                await event_queue.put(("graph_done", None))
            except Exception as e:
                await event_queue.put(("graph_error", e))

        runner_task = asyncio.create_task(_graph_runner())

        try:
            while True:
                item = await event_queue.get()
                if isinstance(item, Event):
                    if item.type == EventType.TOOL_ACTIVITY:
                        payload = item.payload
                        status = payload.get("status")
                        if status == "completed":
                            thinking_log.append(f"[TOOL] {payload.get('tool_name')}({json.dumps(payload.get('tool_args', {}))}) -> completed")
                        elif status == "failed":
                            thinking_log.append(f"[TOOL] {payload.get('tool_name')}({json.dumps(payload.get('tool_args', {}))}) -> failed")
                    yield emit_event(item)
                    continue
                    
                source, data = item
                if source == "graph_done":
                    break
                if source == "graph_error":
                    raise data
                    
                event = data
                for node_name, state_update in event.items():
                    if node_name == "__interrupt__":
                        intr_val = state_update[0].value
                        if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                            yield Event(
                                type=EventType.TOOL_APPROVAL_REQUEST,
                                request_id=request_id,
                                payload={
                                    "tool_name": intr_val.get("tool_name"),
                                    "args": intr_val.get("args"),
                                    "task_id": task_id,
                                    "approval_key": request_id,
                                    "task_class": intr_val.get("task_class"),
                                    "workspace_roots": workspace_roots or [],
                                },
                            )
                            interrupted = True
                            break
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "supervisor" and "delegation_log" in state_update:
                        log_entry = state_update["delegation_log"][-1]
                        decision = log_entry.get("decision", {})
                        next_step = decision.get("next", "UNKNOWN")
                        reason = decision.get("reason", "")
                        briefing = decision.get("briefing", "")
                        
                        text_delta = f"\n\n[Supervisor] -> Delegating to {next_step.upper()}\nReason: {reason}"
                        if briefing:
                            text_delta += f"\nBriefing: {briefing}"
                        text_delta += "\n\n"
                        
                        thinking_log.append(text_delta.strip())
                        
                        yield emit_event(Event(
                            type=EventType.NODE_ACTIVITY,
                            request_id=request_id,
                            payload={
                                "task_id": task_id,
                                "from_node": "supervisor",
                                "to_node": next_step.lower(),
                                "action": "delegation",
                                "reason": reason,
                                "briefing": briefing,
                                "input_payload": {
                                    "user_prompt": prompt,
                                    "plan_context": state_update.get("plan", ""),
                                },
                                "output_payload": {
                                    "decision": decision,
                                    "briefing": briefing,
                                    "assigned_specialist": next_step.lower(),
                                    "updated_plan": state_update.get("plan", ""),
                                },
                                "status": "completed",
                                "timestamp": int(time.time()),
                            },
                        ))
                    elif "messages" in state_update and isinstance(state_update["messages"], list):
                        for msg in state_update["messages"]:
                            if msg.get("role") == "assistant":
                                if msg.get("content"):
                                    full_response_parts.append(msg["content"])
                                    yield emit_event(Event(
                                        type=EventType.TASK_PROGRESS,
                                        request_id=request_id,
                                        payload={
                                            "task_id": task_id,
                                            "text_delta": msg["content"],
                                            "full_text": msg["content"],
                                            "turn": 0,
                                            "node": "assistant" if not msg.get("tool_calls") else node_name,
                                        },
                                    ))
                                if msg.get("tool_calls"):
                                    for tc in msg.get("tool_calls", []):
                                        tool_name = tc.get("function", {}).get("name", "unknown")
                                        args_str = tc.get("function", {}).get("arguments", "{}")
                                        try:
                                            tool_args = json.loads(args_str) if args_str else {}
                                        except Exception:
                                            tool_args = {}
                                            
                                        thinking_log.append(f"[TOOL] {tool_name}({args_str}) -> pending")
                                        yield emit_event(Event(
                                            type=EventType.TOOL_ACTIVITY,
                                            request_id=request_id,
                                            payload={
                                                "task_id": task_id,
                                                "tool_name": tool_name,
                                                "tool_args": tool_args,
                                                "status": "pending",
                                                "node": node_name,
                                                "from_node": node_name,
                                                "to_node": "execute_tool",
                                                "timestamp": int(time.time()),
                                            },
                                        ))
                                elif msg.get("content"):
                                    yield emit_event(Event(
                                        type=EventType.NODE_ACTIVITY,
                                        request_id=request_id,
                                        payload={
                                            "task_id": task_id,
                                            "from_node": node_name,
                                            "to_node": "supervisor",
                                            "action": "step_completed",
                                            "reason": f"{node_name.capitalize()} completed reasoning and formulated output.",
                                            "input_payload": {
                                                "phase": state_update.get("current_phase", "active"),
                                            },
                                            "output_payload": {
                                                "response": msg.get("content", ""),
                                                "plan": state_update.get("plan", ""),
                                            },
                                            "status": "completed",
                                            "timestamp": int(time.time()),
                                        },
                                    ))
                            elif msg.get("role") == "tool":
                                # Handle specialist tool calls by emitting TOOL_ACTIVITY and NODE_ACTIVITY
                                tool_name = msg.get("name", "unknown")
                                tool_args = msg.get("args", {})
                                tool_result = msg.get("content", "")
                                
                                # Query state to find active specialist destination
                                current_state_obj = await graph.aget_state(config)
                                dest_node = current_state_obj.values.get("active_specialist") or "planner"
                                
                                is_failed = isinstance(tool_result, str) and (tool_result.startswith("Error") or tool_result.startswith("Validation failed"))
                                status_str = "failed" if is_failed else "completed"
                                
                                thinking_log.append(f"[TOOL] {tool_name}({json.dumps(tool_args)}) -> {status_str}")
                                
                                yield emit_event(Event(
                                    type=EventType.TOOL_ACTIVITY,
                                    request_id=request_id,
                                    payload={
                                        "task_id": task_id,
                                        "tool_name": tool_name,
                                        "tool_args": tool_args,
                                        "status": status_str,
                                        "result": tool_result,
                                        "node": dest_node,
                                        "from_node": dest_node,
                                        "to_node": "execute_tool",
                                        "timestamp": int(time.time()),
                                    },
                                ))
                    
                    # Check if it was executing a tool
                    if node_name == "execute_tool":
                        current_state_obj = await graph.aget_state(config)
                        current_state = current_state_obj.values
                        messages = current_state.get("messages", [])
                        if messages and messages[-1].get("role") == "tool":
                            pass # Handled above
                if interrupted:
                    break

            if not interrupted:
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
            full_response = "\n".join(full_response_parts).strip()
            
            # Save the episode memory asynchronously if finished
            if not interrupted and full_response:
                try:
                    thinking_log_str = "\n".join(thinking_log)
                    await episodic.store_episode(
                        task_id=task_id,
                        user_prompt=prompt,
                        task_summary=full_response[:200],  # using response snippet as summary
                        tags=["task", "completed"],
                        outcome=full_response,
                        thinking_log=thinking_log_str,
                    )
                except Exception as db_err:
                    logger.error(f"Failed to store episode in DB for {task_id}: {db_err}")
                    
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=request_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.SUCCEEDED.value,
                    "latency_ms": elapsed_ms,
                    "response": full_response or "(task completed)",
                    "thoughts": accumulated_thoughts,
                    "session_id": session_id,
                },
            )
        except Exception as e:
            logger.exception("LangGraph task failed")
            elapsed_ms = round((time.time() - start_time) * 1000, 2) if 'start_time' in locals() else 0.0
            logger.error(f"Graph execution failed for task {task_id}: {e}", exc_info=True)
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={"task_id": task_id, "error": str(e)},
            )
        finally:
            event_bus.unsubscribe(task_id, event_queue)
            runner_task.cancel()

async def resume_langgraph_task(
    thread_id: str,
    approved: bool,
    provider: BaseProvider,
    tools: Optional[ToolRegistry] = None,
    workspace_roots: Optional[List[str]] = None,
    disabled_nodes: Optional[set] = None,
) -> AsyncGenerator[Event, None]:
    """Resume an interrupted LangGraph task and yield remaining events."""
    db_path = Path.home() / ".aether" / "checkpoints.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        
        config = {
            "configurable": {
                "thread_id": thread_id,
                "tool_registry": tools,
                "request_id": thread_id,
                "workspace_roots": workspace_roots or [],
                "task_class": "standard",
                "provider": provider,
                "disabled_nodes": disabled_nodes or set(),
            }
        }
        
        start_time = time.time()
        full_response_parts: list[str] = []
        interrupted = False
        task_id = "unknown"
        
        try:
            state_obj = await graph.aget_state(config)
            task_id = state_obj.values.get("task_id", thread_id)
            
            async for event in graph.astream(Command(resume=approved), config=config, stream_mode="updates"):
                for node_name, state_update in event.items():
                    if node_name == "__interrupt__":
                        intr_val = state_update[0].value
                        if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                            yield Event(
                                type=EventType.TOOL_APPROVAL_REQUEST,
                                request_id=thread_id,
                                payload={
                                    "tool_name": intr_val.get("tool_name"),
                                    "args": intr_val.get("args"),
                                    "task_id": task_id,
                                    "approval_key": thread_id,
                                    "task_class": intr_val.get("task_class"),
                                    "workspace_roots": workspace_roots or [],
                                },
                            )
                            interrupted = True
                            break
                    if not isinstance(state_update, dict):
                        continue
                    if node_name == "supervisor" and "delegation_log" in state_update:
                        log_entry = state_update["delegation_log"][-1]
                        decision = log_entry.get("decision", {})
                        next_step = decision.get("next", "UNKNOWN")
                        reason = decision.get("reason", "")
                        yield Event(
                            type=EventType.TASK_PROGRESS,
                            request_id=thread_id,
                            payload={
                                "task_id": task_id,
                                "text_delta": f"\\n\\n[Supervisor] -> Delegating to {next_step.upper()}\\nReason: {reason}\\n\\n",
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
                                    request_id=thread_id,
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
                                    request_id=thread_id,
                                    payload={
                                        "task_id": task_id,
                                        "text_delta": f"\\n\\n[Tool Executed: {msg.get('name')}]\\n",
                                        "full_text": f"\\n\\n[Tool Executed: {msg.get('name')}]\\n",
                                        "turn": 0,
                                        "node": node_name,
                                    },
                                )
                if interrupted:
                    break

            if not interrupted:
                elapsed_ms = round((time.time() - start_time) * 1000, 2)
                full_response = "\\n".join(full_response_parts).strip()
                
                orig_prompt = state_obj.values.get("original_prompt", "")
                try:
                    await episodic.store_episode(task_id, orig_prompt, full_response, ["task", "completed", "resumed"], TaskState.SUCCEEDED.value)
                    await knowledge_graph.extract_and_store_facts(
                        provider=provider,
                        user_prompt=orig_prompt,
                        assistant_response=full_response,
                        task_id=task_id
                    )
                except Exception as mem_e:
                    logger.warning(f"Memory storage failed: {mem_e}")
                    
                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=thread_id,
                    payload={
                        "task_id": task_id,
                        "state": TaskState.SUCCEEDED.value,
                        "latency_ms": elapsed_ms,
                        "response": full_response or "(task completed)",
                    },
                )
        except Exception as e:
            logger.exception(f"LangGraph task failed during resume with error: {e}")
            elapsed_ms = round((time.time() - start_time) * 1000, 2)
            retriable = _is_retriable_error(e)
            yield Event(
                type=EventType.TASK_FAILED,
                request_id=thread_id,
                payload={
                    "task_id": task_id,
                    "state": TaskState.FAILED.value,
                    "error": str(e),
                    "retriable": retriable,
                    "latency_ms": elapsed_ms,
                },
            )

async def get_pending_approvals() -> List[Dict[str, Any]]:
    """Query the checkpointer for threads blocked on an approval request."""
    db_path = Path.home() / ".aether" / "checkpoints.db"
    pending = []
    
    # We gracefully skip if the DB isn't initialized yet
    if not db_path.exists():
        return pending
        
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        
        # We need aiosqlite to query thread_ids, or we can use the saver if it exposes it
        # AsyncSqliteSaver doesn't expose a list_threads natively in 1.0.1 in a simple way.
        # But we can query the sqlite DB directly.
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT DISTINCT thread_id FROM checkpoints") as cursor:
                threads = [row[0] async for row in cursor]
                
        for t_id in threads:
            try:
                config = {"configurable": {"thread_id": t_id}}
                state = await graph.aget_state(config)
                for task in state.tasks:
                    if task.interrupts:
                        intr_val = task.interrupts[0].value
                        if isinstance(intr_val, dict) and intr_val.get("type") == "approval_request":
                            pending.append({
                                "thread_id": t_id,
                                "task_id": state.values.get("task_id", t_id),
                                "tool_name": intr_val.get("tool_name"),
                                "args": intr_val.get("args"),
                                "task_class": intr_val.get("task_class", "standard")
                            })
                            break
            except Exception as e:
                logger.warning(f"Error checking thread {t_id}: {e}")
                
    return pending
