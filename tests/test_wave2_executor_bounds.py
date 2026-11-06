"""Wave 2, N-06 + N-34: executor/event-bus bounds.

N-06 [S2]: the graph consumer loop waited on
``await event_queue.get()`` with NO timeout, and the ``finally``
only called ``runner_task.cancel()`` — never awaited it. A hung
``astream`` therefore never produced ``graph_done``/``graph_error``
(the task never failed), and on abandonment the un-awaited runner
kept executing provider/tool work after unsubscribe.

N-34 [S3]: the queues between graph, bus and websocket were
unbounded (``asyncio.Queue()`` with no maxsize, ``put_nowait`` with
no drop policy), so a slow or dead consumer meant events
accumulated in memory for the life of the run.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aether_common.contracts import EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph import executor


def _runner_tasks() -> list[asyncio.Task]:
    return [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and getattr(t.get_coro(), "__name__", "") == "_graph_runner"
    ]


async def _settle_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


class _FakeGraph:
    """Graph stub: ``astream`` behaviour is injected per test."""

    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    def astream(self, segment_input, config=None, stream_mode=None):
        return self._stream_factory()

    async def aget_state(self, config):
        return SimpleNamespace(values={}, config={})


@pytest.fixture
def _executor_env(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "DEFAULT_CHECKPOINT_DB",
                        tmp_path / "checkpoints.db")
    monkeypatch.setattr(executor, "GRAPH_EVENT_IDLE_TIMEOUT_SECONDS", 0.5)
    provider = SimpleNamespace(provider_name="fake", model="fake-model")
    return provider


def _install_graph(monkeypatch, graph):
    monkeypatch.setattr(executor, "compile_graph",
                        lambda checkpointer=None: graph)


# ---------------------------------------------------------------------
# N-34 — bus publish drops instead of growing without limit
# ---------------------------------------------------------------------

async def test_event_bus_publish_drops_when_subscriber_queue_full():
    queue: asyncio.Queue = asyncio.Queue(
        maxsize=executor.EVENT_QUEUE_MAXSIZE)
    task_id = "wave2-n34-test"
    event_bus.subscribe(task_id, queue)
    try:
        for _ in range(executor.EVENT_QUEUE_MAXSIZE):
            event_bus.publish(task_id, object())
        assert queue.full()
        # A stalled consumer must not block or crash the publisher,
        # and the queue must not grow past its bound.
        event_bus.publish(task_id, object())
        event_bus.publish(task_id, object())
        assert queue.qsize() == executor.EVENT_QUEUE_MAXSIZE
    finally:
        event_bus.unsubscribe(task_id, queue)


# ---------------------------------------------------------------------
# N-06 — a hung graph fails the task; an abandoned run reaps its runner
# ---------------------------------------------------------------------

async def test_hung_graph_fails_task_instead_of_hanging(
        _executor_env, monkeypatch):
    async def _hung_stream():
        await asyncio.Event().wait()  # never yields, never ends
        yield {}  # pragma: no cover

    _install_graph(monkeypatch, _FakeGraph(_hung_stream))
    events = []

    async def _consume():
        async for ev in executor.run_langgraph_task(
                "do something", _executor_env, task_id="wave2-hang"):
            events.append(ev)

    await asyncio.wait_for(_consume(), timeout=20)
    types = [ev.type for ev in events]
    assert EventType.TASK_FAILED in types, types
    # The runner was reaped, not left executing after the failure.
    assert await _settle_until(lambda: not _runner_tasks())


async def test_abandoned_run_reaps_its_runner(
        _executor_env, monkeypatch):
    entered = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def _slow_wind_down_stream():
        entered.set()
        try:
            yield {"supervisor": {"messages": []}}
            await asyncio.Event().wait()  # hang mid-run
        finally:
            # A runner whose wind-down takes real time (closing a
            # stream, finishing a tool call's teardown).
            await asyncio.sleep(0.3)
            cleanup_done.set()

    _install_graph(monkeypatch, _FakeGraph(_slow_wind_down_stream))

    async def _consume():
        async for _ev in executor.run_langgraph_task(
                "do something", _executor_env, task_id="wave2-abandon"):
            pass

    consumer = asyncio.create_task(_consume())
    assert await _settle_until(entered.is_set)
    await asyncio.sleep(0.1)  # runner is parked inside the stream
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    # The instant the consumer is done, the runner's wind-down must
    # already be complete: the executor's teardown AWAITED it
    # (bounded) instead of firing cancel() and walking away.
    assert cleanup_done.is_set()
    assert await _settle_until(lambda: not _runner_tasks())
