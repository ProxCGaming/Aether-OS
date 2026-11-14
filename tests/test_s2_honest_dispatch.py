"""Master build S2 — honest dispatch for valid-but-undispatched types.

Before S2 the receive loop was:

    handler = _DISPATCH.get(msg.type)
    if handler is not None:
        await handler(conn, msg)

with no else: a message that parses as a valid EventType but has no
dispatch entry (shell/engine version skew is the realistic source)
got no ERROR reply and no log — the UI waited forever. S2 adds the
else: an ERROR reply naming the type, plus a warning log, and the
socket survives.

TASK_INTERRUPTED is the probe type: it is a valid EventType (an
engine -> UI push), it is deliberately not in _DISPATCH, and it is
not one of the receive loop's special cases (STEER_TASK).
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state


def _read_until(ws, wanted, bound=25, timeout=20):
    """Read events until one of ``wanted`` arrives (daemon-thread
    bounded, same pattern as tests/test_wave1_ws_loop_hardening.py:
    a response that never arrives fails instead of hanging)."""
    box = {}

    def _run():
        seen = []
        try:
            for _ in range(bound):
                ev = Event.from_json(ws.receive_text())
                seen.append(ev.type)
                if ev.type in wanted:
                    box["event"] = ev
                    break
            box["seen"] = seen
        except Exception as e:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = e
            box["seen"] = seen

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(
            f"Timed out waiting for {wanted}; saw {box.get('seen')}")
    if "error" in box:
        raise box["error"]
    if "event" not in box:
        raise AssertionError(
            f"Never received any of {wanted}; saw {box.get('seen')}")
    return box["event"], box.get("seen", [])


class TestUndispatchedTypeIsAnswered(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.token_file = self.tmp_path / "engine.token"
        self.audit_file = self.tmp_path / "audit.log"

        engine_state.token_path = self.token_file
        engine_state.audit_logger.log_path = self.audit_file
        self.token = engine_state.initialize()

        self.patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        return client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})

    def test_probe_type_is_really_undispatched(self):
        # Guard the probe's premise: if TASK_INTERRUPTED ever gains a
        # dispatch entry (or a loop special case), this test must be
        # re-pointed at a genuinely undispatched type instead of
        # silently passing against a real handler.
        from aether_engine.routes import websocket
        self.assertNotIn(EventType.TASK_INTERRUPTED, websocket._DISPATCH)

    def test_undispatched_type_gets_error_reply_log_and_live_socket(self):
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            with self.assertLogs(
                    "aether_engine.routes.websocket", level="WARNING") as logs:
                ws.send_text(Event(
                    type=EventType.TASK_INTERRUPTED,
                    request_id="s2-probe-1",
                    payload={"task_id": "t-probe"},
                ).to_json())
                ev, _ = _read_until(ws, {EventType.ERROR})
            self.assertEqual(ev.type, EventType.ERROR)
            self.assertEqual(ev.request_id, "s2-probe-1")
            self.assertIn("TASK_INTERRUPTED", ev.payload.get("error", ""))
            self.assertTrue(
                any("TASK_INTERRUPTED" in line for line in logs.output),
                f"warning log must name the type; got {logs.output}")
            # The socket survives the refused message.
            ws.send_text(Event(type=EventType.PING).to_json())
            pong, _ = _read_until(ws, {EventType.PONG})
            self.assertEqual(pong.type, EventType.PONG)


if __name__ == "__main__":
    unittest.main()
