"""Wave 5 craft: executor's streaming helpers split (step 2).

``langgraph.executor`` used to define the stream-interpretation
helpers inline and keep the resume path's segment-merge pump
(E1-F16) as a closure inside ``resume_langgraph_task``. The
helpers and the pump now live in ``langgraph.streaming``; the
Epic 1 parity suites pin the run/resume behaviour end to end,
but they do not pin the split's surface contract:

- executor's namespace is the historical import surface, so every
  moved name must remain reachable through it as the SAME object
  the new module defines (``langgraph.fast_chat`` imports
  ``_is_retriable_error`` through executor), and
- ``merge_segment_items`` must keep the closure's exact merge
  semantics: bus items forwarded untouched, graph updates tagged
  in arrival order, a graph failure re-raised at the point the
  merge reaches it (after the items that preceded it), and the
  graph stream closed when the consumer stops early.
"""
import asyncio
from types import SimpleNamespace

import pytest

from aether_engine.langgraph import executor, streaming
from aether_engine.providers.base import ProviderError

STREAMING_NAMES = [
    "_SUPERSTEP_LABELS",
    "_is_retriable_error",
    "_superstep_label",
    "merge_segment_items",
]


@pytest.mark.parametrize("name", STREAMING_NAMES)
def test_streaming_names_reexported_identically(name):
    assert getattr(executor, name) is getattr(streaming, name)


def test_superstep_label_shapes():
    assert streaming._superstep_label({"planner": {}}, 3) == "After Planner"
    assert streaming._superstep_label({"execute_tool": {}}, 1) == (
        "After Tool execution")
    assert streaming._superstep_label({"some_node": {}}, 2) == (
        "After Some node")
    assert streaming._superstep_label({"__interrupt__": ()}, 7) == "Step 7"


def test_is_retriable_error_classification():
    assert streaming._is_retriable_error(
        ProviderError("boom", retriable=True)) is True
    assert streaming._is_retriable_error(
        ProviderError("boom", retriable=False)) is False
    assert streaming._is_retriable_error(
        Exception("503 Service Unavailable")) is True
    assert streaming._is_retriable_error(
        Exception("invalid argument")) is False


class _FakeStream:
    """An async-iterable stream recording how it was driven.

    Mirrors the one surface the merge pump relies on: async
    iteration plus an ``aclose`` the pump must call on teardown.
    """

    def __init__(self, script):
        # script: list of items; an Exception instance is raised
        # when reached, the string "gate" waits for the gate event.
        self._script = list(script)
        self.gate = asyncio.Event()
        self.closed = False
        self.calls = []

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        try:
            for item in self._script:
                if item == "gate":
                    await self.gate.wait()
                elif isinstance(item, Exception):
                    raise item
                else:
                    yield item
        finally:
            self.closed = True

    async def aclose(self):
        self.closed = True


class _FakeGraph:
    def __init__(self, stream):
        self._stream = stream

    def astream(self, segment_input, config=None, stream_mode=None):
        self._stream.calls.append((segment_input, config, stream_mode))
        return self._stream


def _graph_with(script):
    stream = _FakeStream(script)
    return _FakeGraph(stream), stream


async def test_merge_forwards_graph_events_and_ends_on_graph_done():
    graph, stream = _graph_with([{"a": 1}, {"b": 2}])
    bus_queue: asyncio.Queue = asyncio.Queue()
    config = {"configurable": {"thread_id": "t"}}
    items = [
        item async for item in streaming.merge_segment_items(
            graph, "input-1", config, bus_queue)
    ]
    assert items == [("graph", {"a": 1}), ("graph", {"b": 2})]
    # The pump drives the graph exactly as the closure did.
    assert stream.calls == [("input-1", config, "updates")]
    assert stream.closed is True


async def test_merge_forwards_bus_items_untouched():
    graph, stream = _graph_with([{"g": 1}, "gate", {"g": 2}])
    bus_queue: asyncio.Queue = asyncio.Queue()
    bus_event = SimpleNamespace(type="THINKING")
    await bus_queue.put(bus_event)

    collected = []

    async def _collect():
        async for item in streaming.merge_segment_items(
                graph, None, {}, bus_queue):
            collected.append(item)
            if len(collected) == 2:
                stream.gate.set()

    await asyncio.wait_for(_collect(), timeout=5)
    # The bus item and the first graph event arrive in pump order;
    # which of the two wins the first slot is a scheduling race,
    # but both precede the gated second graph event.
    first_two = collected[:2]
    assert ("graph", {"g": 1}) in first_two
    # Forwarded untouched: the identical object, not a copy.
    assert any(
        kind == "bus" and data is bus_event for kind, data in first_two)
    assert collected[2] == ("graph", {"g": 2})


async def test_merge_reraises_graph_error_at_the_merge_point():
    graph, _stream = _graph_with([{"g": 1}, RuntimeError("boom")])
    bus_queue: asyncio.Queue = asyncio.Queue()
    collected = []
    with pytest.raises(RuntimeError, match="boom"):
        async for item in streaming.merge_segment_items(
                graph, None, {}, bus_queue):
            collected.append(item)
    # The item produced before the failure is delivered first; the
    # error surfaces only when the merge reaches it.
    assert collected == [("graph", {"g": 1})]


async def test_merge_early_stop_closes_the_graph_stream():
    graph, stream = _graph_with([{"g": 1}, "gate", {"g": 2}])
    bus_queue: asyncio.Queue = asyncio.Queue()
    merge = streaming.merge_segment_items(graph, None, {}, bus_queue)
    first = await asyncio.wait_for(merge.__anext__(), timeout=5)
    assert first == ("graph", {"g": 1})
    await merge.aclose()
    # Pump teardown runs the stream's own close, as the closure's
    # cancelled _pump_graph finally did.
    await asyncio.wait_for(_until_closed(stream), timeout=5)
    assert stream.closed is True


async def _until_closed(stream):
    while not stream.closed:
        await asyncio.sleep(0.01)
