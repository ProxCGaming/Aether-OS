import unittest, json, uuid, time
from aether_common.contracts import (
    SCHEMA_VERSION, EventType, TaskState, Event, validate_transition,
    SchemaVersionMismatchError, InvalidStateTransitionError, InvalidEventPayloadError,
)


class TestCommonContracts(unittest.TestCase):
    def test_task_state_legal_transitions(self):
        validate_transition(TaskState.PENDING, TaskState.RUNNING)
        validate_transition(TaskState.PENDING, TaskState.CANCELLED)
        validate_transition(TaskState.RUNNING, TaskState.SUCCEEDED)
        validate_transition(TaskState.RUNNING, TaskState.FAILED)
        validate_transition(TaskState.RUNNING, TaskState.CANCELLED)

    def test_task_state_illegal_transitions_raise(self):
        for cur, tgt in [(TaskState.PENDING, TaskState.SUCCEEDED),
                         (TaskState.PENDING, TaskState.FAILED),
                         (TaskState.RUNNING, TaskState.PENDING)]:
            with self.assertRaises(InvalidStateTransitionError):
                validate_transition(cur, tgt)

    def test_terminal_task_states_have_no_outgoing_transitions(self):
        for term in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
            for target in TaskState:
                with self.assertRaises(InvalidStateTransitionError):
                    validate_transition(term, target)

    def test_event_serialization_and_deserialization(self):
        ev = Event(type=EventType.TASK_PROGRESS,
                   payload={"progress": 42.5, "step": 3},
                   event_id="test-id-123", ts=1700000000.0)

        j = ev.to_json()
        d = json.loads(j)
        self.assertEqual(d["type"], "TASK_PROGRESS")
        self.assertEqual(d["payload"]["progress"], 42.5)
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)

        r = Event.from_json(j)
        self.assertEqual(r.type, EventType.TASK_PROGRESS)
        self.assertEqual(r.payload, {"progress": 42.5, "step": 3})
        self.assertEqual(r.event_id, "test-id-123")
        self.assertEqual(r.ts, 1700000000.0)

    def test_schema_version_mismatch_raises_loudly(self):
        bad = json.dumps({"type": "HELLO", "payload": {}, "schema_version": 999})
        with self.assertRaises(SchemaVersionMismatchError):
            Event.from_json(bad)

        missing = json.dumps({"type": "HELLO", "payload": {}})
        with self.assertRaises(SchemaVersionMismatchError):
            Event.from_json(missing)

    def test_invalid_event_type_raises(self):
        bad = json.dumps({"type": "UNKNOWN_XYZ", "payload": {}, "schema_version": SCHEMA_VERSION})
        with self.assertRaises(InvalidEventPayloadError):
            Event.from_json(bad)

    def test_malformed_json_raises(self):
        with self.assertRaises(InvalidEventPayloadError):
            Event.from_json("not-valid-json {[[")

    def test_provider_event_types_roundtrip(self):
        """Phase 3: new provider management event types serialize correctly."""
        provider_types = [
            EventType.PROVIDER_LIST_REQUEST, EventType.PROVIDER_LIST_RESPONSE,
            EventType.PROVIDER_VALIDATE_REQUEST, EventType.PROVIDER_VALIDATE_RESPONSE,
            EventType.PROVIDER_SAVE_REQUEST, EventType.PROVIDER_SAVE_RESPONSE,
            EventType.PROVIDER_REMOVE_REQUEST, EventType.PROVIDER_REMOVE_RESPONSE,
            EventType.MODEL_SET_DEFAULT, EventType.MODEL_DEFAULT_CHANGED,
        ]
        for etype in provider_types:
            ev = Event(type=etype, payload={"test": True})
            j = ev.to_json()
            restored = Event.from_json(j)
            self.assertEqual(restored.type, etype)
            self.assertEqual(restored.payload["test"], True)


if __name__ == "__main__":
    unittest.main()

