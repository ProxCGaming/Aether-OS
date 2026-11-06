"""Stream interpretation and the resume segment-merge pump.

Split out of ``langgraph/executor.py`` in the Wave 5 craft pass:
the superstep labelling and the error-retryability classification
are interpretation helpers both the run and resume paths share,
and ``merge_segment_items`` is the pump that merges one graph
segment's updates with the task's event-bus queue (E1-F16) — it
was a closure inside ``resume_langgraph_task``'s segment consumer,
and the lift to this module is a pure parameterisation (the
captured ``graph`` / ``config`` / ``bus_queue`` are now arguments;
the queue discipline, the pump teardown and the raise point are
unchanged). ``langgraph.executor`` re-exports every name defined
here; its namespace is the historical import and patch surface
(``langgraph.fast_chat`` imports ``_is_retriable_error`` through
it).
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from aether_engine.event_bus import EVENT_QUEUE_MAXSIZE
from aether_engine.providers.base import (
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)

_SUPERSTEP_LABELS = {
    "supervisor": "Supervisor",
    "planner": "Planner",
    "coder": "Coder",
    "researcher": "Researcher",
    "execute_tool": "Tool execution",
}


def _superstep_label(event: dict, step_index: int) -> str:
    for key in event:
        if not key.startswith("__"):
            pretty = _SUPERSTEP_LABELS.get(
                key, key.replace("_", " ").capitalize())
            return f"After {pretty}"
    return f"Step {step_index}"


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


async def merge_segment_items(
    graph: Any,
    segment_input: Any,
    config: Any,
    bus_queue: asyncio.Queue,
) -> AsyncGenerator[tuple[str, Any], None]:
    """Yield one graph segment's updates merged with the bus queue.

    E1-F16: this segment's graph updates merged with the task's bus
    queue: bus items arrive as ("bus", Event) and are forwarded
    untouched — THINKING, SCRATCHPAD and artifact/media notices
    reach the resume consumer in arrival order, as they do on the
    run path — while graph updates keep the caller's translation,
    unchanged. A graph failure re-raises at the point the merge
    reaches it, exactly where a direct astream raise landed.
    """
    merged: asyncio.Queue = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)

    async def _pump_graph():
        stream = graph.astream(
            segment_input, config=config, stream_mode="updates")
        try:
            async for graph_event in stream:
                await merged.put(("graph", graph_event))
        except Exception as exc:  # noqa: BLE001 — re-raised by the merge at the matching point below.
            await merged.put(("graph_error", exc))
        finally:
            stream_aclose = getattr(stream, "aclose", None)
            if stream_aclose is not None:
                await stream_aclose()
            await merged.put(("graph_done", None))

    async def _pump_bus():
        while True:
            bus_event = await bus_queue.get()
            await merged.put(("bus", bus_event))

    pumps = [
        asyncio.create_task(_pump_graph()),
        asyncio.create_task(_pump_bus()),
    ]
    try:
        while True:
            kind, data = await merged.get()
            if kind == "graph_done":
                return
            if kind == "graph_error":
                raise data
            yield kind, data
    finally:
        for pump in pumps:
            pump.cancel()
