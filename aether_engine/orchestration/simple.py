"""Multi-turn streaming task orchestrator with tool execution and strict fallback boundaries."""
import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid

from aether_common.contracts import (
    Event,
    EventType,
    TaskState,
    validate_transition,
)
from aether_engine.providers.base import (
    BaseProvider,
    ProviderError,
    StreamChunk,
    ToolCall,
)
from aether_engine.routing.fallback import classify_provider_error
from aether_engine.tools.registry import ToolRegistry
from aether_engine.validation.pre_flight import validate_tool_request

logger = logging.getLogger("aether_engine.orchestration")


async def run_task(
    prompt: str,
    provider: BaseProvider,
    tools: Optional[ToolRegistry] = None,
    task_id: Optional[str] = None,
    request_id: Optional[str] = None,
    max_turns: int = 5,
    approval_handler: Optional[Any] = None,
    workspace_roots: Optional[List[str]] = None,
) -> AsyncGenerator[Event, None]:
    """Execute a task prompt against an LLM provider with multi-turn on-demand tool execution."""
    task_id = task_id or str(uuid.uuid4())
    state = TaskState.PENDING

    # Transition PENDING -> RUNNING
    validate_transition(state, TaskState.RUNNING)
    state = TaskState.RUNNING

    yield Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={"task_id": task_id, "prompt": prompt, "state": state.value},
    )

    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_defs = tools.get_definitions() if tools else []
    start_time = time.time()

    try:
        for turn in range(max_turns):
            turn_text: List[str] = []
            turn_tool_calls: List[ToolCall] = []

            async for chunk in provider.call_stream(messages, tool_defs):
                if chunk.text:
                    turn_text.append(chunk.text)
                    yield Event(
                        type=EventType.TASK_PROGRESS,
                        request_id=request_id,
                        payload={
                            "task_id": task_id,
                            "text_delta": chunk.text,
                            "full_text": "".join(turn_text),
                            "turn": turn,
                        },
                    )
                if chunk.tool_calls:
                    turn_tool_calls.extend(chunk.tool_calls)

            if turn_tool_calls:
                # Append assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": "".join(turn_text),
                    "tool_calls": turn_tool_calls,
                })

                # Execute requested tools on-demand
                for tc in turn_tool_calls:
                    yield Event(
                        type=EventType.TASK_PROGRESS,
                        request_id=request_id,
                        payload={
                            "task_id": task_id,
                            "tool_call": tc.name,
                            "args": tc.args,
                        },
                    )

                    if not tools:
                        result = f"Error: Tool '{tc.name}' not found (no tool registry configured)."
                    else:
                        validation = validate_tool_request(tc.name, tc.args, workspace_roots=workspace_roots)
                        if not validation.is_valid:
                            logger.warning(f"Pre-flight validation failed for tool '{tc.name}': {validation.error}")
                            result = f"Validation failed: {validation.error}"
                        else:
                            requires_approval = tc.name in {"write_file", "delete_file", "execute_shell"}
                            if requires_approval and approval_handler is not None:
                                approved = await approval_handler(tc.name, tc.args, request_id)
                                if not approved:
                                    result = f"Tool approval rejected for '{tc.name}'."
                                else:
                                    try:
                                        result = await tools.execute(tc.name, tc.args)
                                    except Exception as te:
                                        logger.error(f"Error executing tool '{tc.name}': {te}")
                                        result = f"Error executing tool '{tc.name}': {te}"
                            else:
                                try:
                                    result = await tools.execute(tc.name, tc.args)
                                except Exception as te:
                                    logger.error(f"Error executing tool '{tc.name}': {te}")
                                    result = f"Error executing tool '{tc.name}': {te}"

                    yield Event(
                        type=EventType.TASK_PROGRESS,
                        request_id=request_id,
                        payload={
                            "task_id": task_id,
                            "tool_result": tc.name,
                            "result": result,
                        },
                    )

                    # Feed tool execution result back into conversation history
                    messages.append({
                        "role": "tool",
                        "name": tc.name,
                        "content": result,
                        "call_id": tc.call_id,
                        "tool_call_id": tc.call_id,
                    })

                # Proceed to next turn with tool results in context
                continue

            # Model completed without further tool calls
            latency_ms = (time.time() - start_time) * 1000.0
            validate_transition(state, TaskState.SUCCEEDED)
            state = TaskState.SUCCEEDED
            yield Event(
                type=EventType.TASK_COMPLETED,
                request_id=request_id,
                payload={
                    "task_id": task_id,
                    "response": "".join(turn_text),
                    "state": state.value,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            return

        # Max turns exceeded
        validate_transition(state, TaskState.FAILED)
        state = TaskState.FAILED
        yield Event(
            type=EventType.TASK_FAILED,
            request_id=request_id,
            payload={
                "task_id": task_id,
                "error": f"Max turn limit ({max_turns}) exceeded without final response.",
                "retriable": False,
                "state": state.value,
            },
        )

    except asyncio.CancelledError:
        logger.info(f"Task {task_id} was cancelled.")
        validate_transition(state, TaskState.CANCELLED)
        state = TaskState.CANCELLED
        yield Event(
            type=EventType.TASK_CANCELLED,
            request_id=request_id,
            payload={"task_id": task_id, "state": state.value},
        )
        raise

    except Exception as e:
        logger.error(f"Task {task_id} failed with error: {e}", exc_info=True)
        validate_transition(state, TaskState.FAILED)
        state = TaskState.FAILED
        is_retriable, msg = classify_provider_error(e)

        # Preserve ADR 0005 trade-off: NEVER fallback automatically mid-tool-loop
        is_mid_tool_loop = len(messages) > 1
        if is_mid_tool_loop:
            is_retriable = False
            error_text = f"Tool loop failed: {e} (Please start a new task to retry with another provider)."
        else:
            error_text = str(e)

        yield Event(
            type=EventType.TASK_FAILED,
            request_id=request_id,
            payload={
                "task_id": task_id,
                "error": error_text,
                "classified_error": msg,
                "retriable": is_retriable,
                "tool_loop": is_mid_tool_loop,
                "state": state.value,
            },
        )
