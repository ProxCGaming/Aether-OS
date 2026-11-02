"""Phase 10.3, D2: direct tests for ``aether_engine.event_bus.EventBus``.

No direct tests existed: the bus is only exercised indirectly through
the supervisor/executor integration tests. The bus is a process-wide
singleton, so every test resets its subscriber table first — a
subscriber leaked from one test would otherwise receive another
test's events.
"""
import asyncio

import pytest

from aether_engine.event_bus import EventBus, event_bus


@pytest.fixture(autouse=True)
def _reset_bus():
    EventBus._instance._subscribers.clear()
    yield
    EventBus._instance._subscribers.clear()


def drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestSingleton:
    def test_event_bus_is_a_singleton(self):
        assert EventBus() is EventBus()
        assert EventBus() is event_bus

    def test_second_construction_does_not_wipe_subscribers(self):
        queue = asyncio.Queue()
        event_bus.subscribe("task-1", queue)
        EventBus()  # __init__ runs again on the same instance
        event_bus.publish("task-1", {"x": 1})
        assert drain(queue) == [{"x": 1}]


class TestPublishSubscribe:
    def test_subscriber_receives_published_event(self):
        queue = asyncio.Queue()
        event_bus.subscribe("task-1", queue)
        event_bus.publish("task-1", {"type": "TOOL_ACTIVITY"})
        assert drain(queue) == [{"type": "TOOL_ACTIVITY"}]

    def test_events_arrive_in_publish_order(self):
        queue = asyncio.Queue()
        event_bus.subscribe("task-1", queue)
        for i in range(5):
            event_bus.publish("task-1", i)
        assert drain(queue) == [0, 1, 2, 3, 4]

    def test_multiple_subscribers_each_get_a_copy(self):
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        event_bus.subscribe("task-1", q1)
        event_bus.subscribe("task-1", q2)
        event_bus.publish("task-1", "evt")
        assert drain(q1) == ["evt"]
        assert drain(q2) == ["evt"]

    def test_publish_to_unknown_task_is_a_no_op(self):
        event_bus.publish("no-such-task", {"type": "X"})  # must not raise

    def test_tasks_are_isolated(self):
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        event_bus.subscribe("task-1", q1)
        event_bus.subscribe("task-2", q2)
        event_bus.publish("task-1", "for-1")
        assert drain(q1) == ["for-1"]
        assert drain(q2) == []

    def test_publish_to_a_full_queue_does_not_raise(self):
        queue = asyncio.Queue(maxsize=1)
        event_bus.subscribe("task-1", queue)
        event_bus.publish("task-1", "first")
        event_bus.publish("task-1", "second")  # put_nowait would raise QueueFull
        assert drain(queue) == ["first"]


class TestUnsubscribe:
    def test_unsubscribe_one_queue_leaves_the_other_subscribed(self):
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        event_bus.subscribe("task-1", q1)
        event_bus.subscribe("task-1", q2)
        event_bus.unsubscribe("task-1", q1)
        event_bus.publish("task-1", "evt")
        assert drain(q1) == []
        assert drain(q2) == ["evt"]

    def test_unsubscribing_the_last_queue_removes_the_task(self):
        queue = asyncio.Queue()
        event_bus.subscribe("task-1", queue)
        event_bus.unsubscribe("task-1", queue)
        assert "task-1" not in event_bus._subscribers
        event_bus.publish("task-1", "evt")  # no-op after removal
        assert drain(queue) == []

    def test_unsubscribe_without_queue_removes_all_subscribers_for_task(self):
        q1, q2 = asyncio.Queue(), asyncio.Queue()
        event_bus.subscribe("task-1", q1)
        event_bus.subscribe("task-1", q2)
        event_bus.unsubscribe("task-1")
        assert "task-1" not in event_bus._subscribers
        event_bus.publish("task-1", "evt")
        assert drain(q1) == []
        assert drain(q2) == []

    def test_unsubscribe_unknown_task_or_queue_is_a_no_op(self):
        queue = asyncio.Queue()
        event_bus.unsubscribe("no-such-task")
        event_bus.unsubscribe("no-such-task", queue)
        event_bus.subscribe("task-1", queue)
        event_bus.unsubscribe("task-1", asyncio.Queue())  # a different queue
        event_bus.publish("task-1", "still-here")
        assert drain(queue) == ["still-here"]
