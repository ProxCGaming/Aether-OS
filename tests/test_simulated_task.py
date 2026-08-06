import unittest
import asyncio
from aether_common.contracts import EventType, TaskState
from aether_engine.simulated_task import SimulatedTask

class TestSimulatedTask(unittest.IsolatedAsyncioTestCase):
    async def test_normal_task_execution(self):
        task = SimulatedTask(total_ticks=3, tick_interval=0.01)
        events = []
        async for event in task.run():
            events.append(event)

        # Event sequence: TASK_CREATED, TASK_PROGRESS(0), TASK_PROGRESS(1), TASK_PROGRESS(2), TASK_PROGRESS(3), TASK_COMPLETED
        self.assertEqual(len(events), 6)
        self.assertEqual(events[0].type, EventType.TASK_CREATED)
        self.assertEqual(events[0].payload["state"], TaskState.RUNNING.value)
        self.assertEqual(events[1].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[1].payload["progress"], 0.0)
        self.assertEqual(events[2].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[2].payload["tick"], 1)
        self.assertEqual(events[3].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[3].payload["tick"], 2)
        self.assertEqual(events[4].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[4].payload["tick"], 3)
        self.assertEqual(events[4].payload["progress"], 100.0)
        self.assertEqual(events[5].type, EventType.TASK_COMPLETED)
        self.assertEqual(events[5].payload["state"], TaskState.SUCCEEDED.value)
        self.assertEqual(task.state, TaskState.SUCCEEDED)

    async def test_task_cancellation_during_run(self):
        task = SimulatedTask(total_ticks=10, tick_interval=0.05)
        events = []

        async def cancel_later():
            await asyncio.sleep(0.08)
            task.cancel()

        cancel_task = asyncio.create_task(cancel_later())
        async for event in task.run():
            events.append(event)
        await cancel_task

        self.assertEqual(task.state, TaskState.CANCELLED)
        last_event = events[-1]
        self.assertEqual(last_event.type, EventType.TASK_CANCELLED)
        self.assertEqual(last_event.payload["state"], TaskState.CANCELLED.value)

if __name__ == "__main__":
    unittest.main()
