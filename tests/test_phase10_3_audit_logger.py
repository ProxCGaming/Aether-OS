"""Phase 10.3, D2: direct tests for ``aether_engine.audit.AuditLogger``.

Phase 10.2's ``test_phase10_2_audit_gaps.py`` covers the audit logger
only indirectly — through secret-store / tool-execution call sites
(key reveal, destructive tool, no full payload). What was untested is
the logger itself: what each ``log_*`` method writes, the append
behaviour, and the redaction rules in ``_redact`` that the Phase 10.2
C2 comment promises ("every log_* method funnels through _append").

Scope note: no hashing, rotation, fsync or locking exists in the
logger (deferred per the C2 comment / critical/07 M3), so none is
tested here — the append test asserts the behaviour that DOES exist:
records accumulate, one JSON object per line, nothing is truncated.
"""
import json
from pathlib import Path

import pytest

from aether_common.contracts import TaskState
from aether_engine.audit import AuditLogger, _redact


@pytest.fixture
def logger(tmp_path):
    return AuditLogger(log_path=tmp_path / "logs" / "audit.log")


def read_records(log_path):
    text = Path(log_path).read_text(encoding="utf-8")
    assert text.strip(), "audit log must not be empty"
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestLogMethods:
    def test_log_connection_records_success_ip_port_reason(self, logger, tmp_path):
        logger.log_connection(True, ip="127.0.0.1", port=54321, reason="ok")
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["type"] == "CONNECTION_ATTEMPT"
        assert record["success"] is True
        assert record["client_ip"] == "127.0.0.1"
        assert record["client_port"] == 54321
        assert record["reason"] == "ok"
        assert isinstance(record["ts"], float)

    def test_log_connection_failure_defaults(self, logger, tmp_path):
        logger.log_connection(False)
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["success"] is False
        assert record["client_ip"] == "unknown"
        assert record["client_port"] is None
        assert record["reason"] == ""

    def test_log_task_transition_records_states_and_shape_only_payload(self, logger, tmp_path):
        logger.log_task_transition(
            "task-1", TaskState.PENDING, TaskState.RUNNING,
            payload={"args": {"command": "rm -rf /", "secret": "x"}, "note": "hi"},
        )
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["type"] == "TASK_TRANSITION"
        assert record["task_id"] == "task-1"
        assert record["from_state"] == "PENDING"
        assert record["to_state"] == "RUNNING"
        # The "payload" key itself is a payload key, so the whole dict
        # becomes shape-only: keys listed, values never written.
        assert record["payload"] == {"keys": ["args", "note"]}
        assert "rm -rf /" not in json.dumps(record)

    def test_log_event_records_action_and_details(self, logger, tmp_path):
        logger.log_event("WORKER_EXECUTION_COMPLETED", {"tool": "write_file"})
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["type"] == "ENGINE_EVENT"
        assert record["action"] == "WORKER_EXECUTION_COMPLETED"
        assert record["details"] == {"tool": "write_file"}

    def test_log_validation_records_tool_validity_and_message(self, logger, tmp_path):
        logger.log_validation("execute_shell", False, "path outside workspace",
                              payload={"path": "/etc/passwd"})
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["type"] == "VALIDATION"
        assert record["tool_name"] == "execute_shell"
        assert record["valid"] is False
        assert record["message"] == "path outside workspace"
        # "payload" is a payload key: shape-only, never the value.
        assert record["payload"] == {"keys": ["path"]}
        assert "/etc/passwd" not in json.dumps(record)

    def test_log_calls_without_optional_payload_write_empty_shape(self, logger, tmp_path):
        logger.log_task_transition("t", TaskState.RUNNING, TaskState.SUCCEEDED)
        logger.log_event("SOMETHING")
        records = read_records(tmp_path / "logs" / "audit.log")
        # An omitted payload is still shape-only: {"keys": []}, not {}.
        assert records[0]["payload"] == {"keys": []}
        assert records[1]["details"] == {}


class TestAppendBehaviour:
    def test_records_append_one_json_object_per_line(self, logger, tmp_path):
        log_path = tmp_path / "logs" / "audit.log"
        logger.log_event("FIRST")
        logger.log_event("SECOND")
        logger.log_event("THIRD")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        # Every line is independently valid JSON (JSONL, not a JSON array).
        actions = [json.loads(line)["action"] for line in lines]
        assert actions == ["FIRST", "SECOND", "THIRD"]

    def test_second_logger_instance_appends_instead_of_truncating(self, tmp_path):
        log_path = tmp_path / "logs" / "audit.log"
        AuditLogger(log_path=log_path).log_event("FIRST")
        AuditLogger(log_path=log_path).log_event("SECOND")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["action"] for line in lines] == ["FIRST", "SECOND"]

    def test_logger_creates_parent_directories(self, tmp_path):
        log_path = tmp_path / "deep" / "nested" / "audit.log"
        AuditLogger(log_path=log_path).log_event("CREATED_DIRS")
        assert log_path.is_file()
        assert json.loads(log_path.read_text(encoding="utf-8"))["action"] == "CREATED_DIRS"

    def test_no_rotation_or_hashing_exists_yet(self, logger, tmp_path):
        # Deferred decision (critical/07 M3): the logger is plain append.
        # Pin the current behaviour so a silent change is caught: many
        # records stay in the single file, unhashed, in order.
        log_path = tmp_path / "logs" / "audit.log"
        for i in range(50):
            logger.log_event(f"EVT_{i:02d}")
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 50
        assert json.loads(lines[0])["action"] == "EVT_00"
        assert json.loads(lines[-1])["action"] == "EVT_49"
        assert "hash" not in json.loads(lines[0])


class TestRedaction:
    def test_sensitive_keys_are_replaced(self):
        redacted = _redact({
            "api_key": "sk-live-123",
            "token": "tok-abc",
            "password": "hunter2",
            "apiKey": "sk-live-456",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----",
            "session_key": "sess-1",
            "author": "arindam",
            "note": "plain text",
        })
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["token"] == "[REDACTED]"
        assert redacted["password"] == "[REDACTED]"
        assert redacted["apiKey"] == "[REDACTED]"
        assert redacted["private_key"] == "[REDACTED]"
        assert redacted["session_key"] == "[REDACTED]"
        # "author" must NOT match: bare "auth" is not a sensitive part.
        assert redacted["author"] == "arindam"
        assert redacted["note"] == "plain text"
        assert "sk-live" not in json.dumps(redacted)

    def test_bare_key_and_key_suffix_are_sensitive(self):
        assert _redact({"key": "v", "x": 1})["key"] == "[REDACTED]"
        assert _redact({"encryption_key": "v"})["encryption_key"] == "[REDACTED]"

    def test_payload_keys_become_shape_only(self):
        redacted = _redact({"args": {"command": "rm -rf /", "path": "/etc"},
                            "payload": {"a": 1, "b": 2},
                            "manifest": {"name": "x"}})
        assert redacted["args"] == {"keys": ["command", "path"]}
        assert redacted["payload"] == {"keys": ["a", "b"]}
        assert redacted["manifest"] == {"keys": ["name"]}

    def test_content_and_command_are_dropped_entirely(self):
        redacted = _redact({"content": "file contents here", "command": "curl -H 'Auth: x'"})
        assert redacted["content"] == "[REDACTED]"
        assert redacted["command"] == "[REDACTED]"

    def test_bare_secret_patterns_in_strings_are_scrubbed(self):
        redacted = _redact({"message": "key is sk-abcdefgh12345678 end",
                            "items": ["AKIAIOSFODNN7EXAMPLE", "plain"]})
        assert "sk-abcdefgh" not in redacted["message"]
        assert "[REDACTED]" in redacted["message"]
        assert redacted["items"][0] == "[REDACTED]"
        assert redacted["items"][1] == "plain"

    def test_redaction_recurses_into_nested_dicts_and_lists(self, logger, tmp_path):
        logger.log_event("NESTED", {"outer": {"api_key": "sk-secret-1",
                                              "inner_list": [{"token": "t-1"}]}})
        (record,) = read_records(tmp_path / "logs" / "audit.log")
        assert record["details"]["outer"]["api_key"] == "[REDACTED]"
        assert record["details"]["outer"]["inner_list"][0]["token"] == "[REDACTED]"

    def test_redaction_never_leaks_via_the_log_file(self, logger, tmp_path):
        logger.log_event("LEAK_CHECK", {"api_key": "sk-topsecret-999",
                                        "args": {"command": "echo pwned"}})
        raw = (tmp_path / "logs" / "audit.log").read_text(encoding="utf-8")
        assert "sk-topsecret-999" not in raw
        assert "echo pwned" not in raw

    def test_non_string_scalars_pass_through_unchanged(self):
        assert _redact({"count": 3, "ok": True, "ratio": 1.5, "nothing": None}) == {
            "count": 3, "ok": True, "ratio": 1.5, "nothing": None}
