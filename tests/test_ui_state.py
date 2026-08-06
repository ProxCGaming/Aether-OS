import unittest
from aether_common.contracts import Event, EventType
from aether_ui.state import UIStateMachine, UIState

class TestUIState(unittest.TestCase):
    def setUp(self):
        self.sm = UIStateMachine()

    def test_initial_state(self):
        self.assertEqual(self.sm.state, UIState.DISCONNECTED)
        self.assertIsNone(self.active_task_id if hasattr(self, 'active_task_id') else self.sm.active_task_id)

    def test_lifecycle_happy_path(self):
        # 1. Connecting
        self.sm.set_connecting()
        self.assertEqual(self.sm.state, UIState.CONNECTING)

        # 2. HELLO -> CONNECTED
        hello_event = Event(type=EventType.HELLO, payload={"message": "Ready"})
        self.sm.transition_on_event(hello_event)
        self.assertEqual(self.sm.state, UIState.CONNECTED)

        # 3. START_TASK -> TASK_CREATED -> TASK_RUNNING
        created_event = Event(
            type=EventType.TASK_CREATED,
            payload={"task_id": "task-abc", "total_ticks": 10},
        )
        self.sm.transition_on_event(created_event)
        self.assertEqual(self.sm.state, UIState.TASK_RUNNING)
        self.assertEqual(self.sm.active_task_id, "task-abc")
        self.assertEqual(self.sm.task_progress, 0.0)

        # 4. TASK_PROGRESS updates
        prog_event = Event(
            type=EventType.TASK_PROGRESS,
            payload={"task_id": "task-abc", "progress": 50.0, "tick": 5, "total_ticks": 10},
        )
        self.sm.transition_on_event(prog_event)
        self.assertEqual(self.sm.state, UIState.TASK_RUNNING)
        self.assertEqual(self.sm.task_progress, 50.0)

        # 5. TASK_COMPLETED -> CONNECTED
        comp_event = Event(type=EventType.TASK_COMPLETED, payload={"task_id": "task-abc"})
        self.sm.transition_on_event(comp_event)
        self.assertEqual(self.sm.state, UIState.CONNECTED)
        self.assertEqual(self.sm.task_progress, 100.0)
        self.assertIsNone(self.sm.active_task_id)

    def test_task_cancelled_transition(self):
        self.sm.state = UIState.TASK_RUNNING
        self.sm.active_task_id = "task-to-cancel"
        
        cancel_event = Event(type=EventType.TASK_CANCELLED, payload={"task_id": "task-to-cancel"})
        self.sm.transition_on_event(cancel_event)
        self.assertEqual(self.sm.state, UIState.CONNECTED)
        self.assertIsNone(self.sm.active_task_id)

    def test_error_transition(self):
        err_event = Event(type=EventType.ERROR, payload={"error": "Something went wrong"})
        self.sm.transition_on_event(err_event)
        self.assertEqual(self.sm.state, UIState.ERROR)
        self.assertEqual(self.sm.error_message, "Something went wrong")

    def test_subscriber_notification(self):
        notified = []
        self.sm.subscribe(lambda sm: notified.append(sm.state))

        self.sm.set_connecting()
        self.sm.transition_on_event(Event(type=EventType.HELLO))
        
        self.assertEqual(notified, [UIState.CONNECTING, UIState.CONNECTED])

if __name__ == "__main__":
    unittest.main()
