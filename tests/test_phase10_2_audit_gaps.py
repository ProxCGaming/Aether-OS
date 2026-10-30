"""Phase 10.2, C2: audit gaps around the trust boundary.

Each test reproduces a finding from critical/07 (M4/M5): it fails on the
pre-fix code and passes after.

  * key-reveal (SecretStore.load_provider — the path the websocket
    PROVIDER_REVEAL_KEY_REQUEST handler and scripts/export_keys.py take)
    produced NO audit event; neither did deleting a stored key.
  * AuditLogger wrote whatever it was handed: a full tool payload
    (APPROVAL_REQUESTED "args", TASK_TRANSITION "payload") and any key
    value inside it landed verbatim in the plaintext logs/audit.log.
  * ToolRegistry.execute() audited only denials (TOOL_BLOCKED_BY_POLICY):
    a successful destructive tool left no registry-level record.

Out of scope (follow-up, critical/07 M3): hashing/rotation/fsync/lock/
sequence on the audit log itself — deliberately NOT built here.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aether_common.contracts import TaskState
from aether_engine.audit import AuditLogger
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.tools.registry import ToolRegistry, create_file_tools

SECRET = "sk-test-C2SECRET-1234567890"
SECRET2 = "sk-test-C2SECRET-0987654321"


class DummyProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


def _events(log_path):
    log = Path(log_path)
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _actions(events):
    return [e.get("action") for e in events if e.get("type") == "ENGINE_EVENT"]


def test_key_reveal_produces_audit_event(tmp_path):
    """load_provider (the key-reveal path) must produce an audit event.

    Pre-fix: SecretStore.load_provider had no audit call at all, so the
    websocket PROVIDER_REVEAL_KEY_REQUEST handler sent the decrypted key
    with no trace (critical/07 M4)."""
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    store = SecretStore(db_path=tmp_path / "secrets.db", protector=DummyProtector(),
                        audit_logger=audit)
    store.save_provider("openai", SECRET)
    assert store.load_provider("openai") == SECRET

    events = _events(tmp_path / "audit.log")
    revealed = [e for e in events if e.get("action") == "SECRET_LOADED"]
    assert len(revealed) == 1, f"expected one SECRET_LOADED event, got: {events}"
    assert revealed[0]["details"] == {"provider": "openai"}
    assert SECRET not in (tmp_path / "audit.log").read_text()


def test_failed_key_load_produces_no_reveal_event(tmp_path):
    """No key was revealed, so no reveal event: not-found / undecryptable
    loads must stay silent (guards against over-auditing)."""
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    store = SecretStore(db_path=tmp_path / "secrets.db", protector=DummyProtector(),
                        audit_logger=audit)
    try:
        store.load_provider("no_such_provider")
    except ProviderNotFoundError:
        pass
    else:
        raise AssertionError("expected ProviderNotFoundError")
    assert "SECRET_LOADED" not in _actions(_events(tmp_path / "audit.log"))


def test_secret_delete_produces_audit_event(tmp_path):
    """Deleting a stored key is a destructive action and must be audited;
    deleting a provider that does not exist must not be (nothing was
    destroyed). Pre-fix: delete_provider had no audit call."""
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    store = SecretStore(db_path=tmp_path / "secrets.db", protector=DummyProtector(),
                        audit_logger=audit)
    store.save_provider("openai", SECRET)
    assert store.delete_provider("openai") is True
    assert store.delete_provider("openai") is False  # already gone

    actions = _actions(_events(tmp_path / "audit.log"))
    assert actions.count("SECRET_DELETED") == 1, f"actions: {actions}"
    assert SECRET not in (tmp_path / "audit.log").read_text()


def test_audit_entry_contains_no_secret_value_or_full_tool_payload(tmp_path):
    """An audit entry for a tool call must contain no secret value and no
    full tool payload. Pre-fix: log_event/log_task_transition wrote the
    caller's dict verbatim, so APPROVAL_REQUESTED "args" (shell command,
    file content, pasted keys) and TASK_TRANSITION "payload" (prompts,
    responses) landed in plaintext in logs/audit.log (critical/07 M5)."""
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    audit.log_event("APPROVAL_REQUESTED", {
        "tool_name": "execute_shell",
        "args": {"command": "curl -H 'Authorization: Bearer " + SECRET + "' https://example.invalid",
                 "api_key": SECRET2, "content": "file contents with " + SECRET},
        "task_id": "t1",
    })
    audit.log_task_transition("t1", TaskState.PENDING, TaskState.RUNNING, {
        "prompt": "summarise this, my key is " + SECRET,
        "response": "here is the secret data",
        "api_key": SECRET,
    })
    audit.log_validation("write_file", False, "validation failed", {
        "path": "x.txt", "content": SECRET, "api_key": SECRET2,
    })
    # A secret passed as a direct value (not inside a payload dict) must
    # be replaced in place, not dropped with its record.
    audit.log_event("PROVIDER_SAVED", {"provider": "openai", "api_key": SECRET2})

    raw = (tmp_path / "audit.log").read_text()
    for secret in (SECRET, SECRET2):
        assert secret not in raw, f"secret value leaked into audit log: {raw}"
    # Full payload values must not survive either — only their shape.
    assert "summarise this" not in raw
    assert "here is the secret data" not in raw
    assert "file contents with" not in raw
    assert "curl -H" not in raw
    # ... but the events themselves, the tool name, and the payload *shape*
    # must still be there (redaction, not deletion of the record).
    assert "[REDACTED]" in raw
    events = _events(tmp_path / "audit.log")
    assert _actions(events) == ["APPROVAL_REQUESTED", "PROVIDER_SAVED"]
    saved = [e for e in events if e.get("action") == "PROVIDER_SAVED"][0]
    assert saved["details"] == {"provider": "openai", "api_key": "[REDACTED]"}
    approval = events[0]["details"]
    assert approval["tool_name"] == "execute_shell"
    assert approval["args"] == {"keys": ["api_key", "command", "content"]}
    transition = [e for e in events if e.get("type") == "TASK_TRANSITION"][0]
    assert transition["payload"] == {"keys": ["api_key", "prompt", "response"]}
    assert transition["from_state"] == "PENDING" and transition["to_state"] == "RUNNING"


def test_destructive_tool_execution_is_audited_without_payload(tmp_path):
    """A successful destructive tool through ToolRegistry.execute() must
    leave a registry-level audit record, with argument names only.
    Pre-fix: the registry audited only denials, so an approved delete_file
    that ran produced no registry-level event at all (the worker layer's
    WORKER_EXECUTION_COMPLETED records the process, not the allow-decision,
    and carries no argument info)."""
    audit = AuditLogger(log_path=tmp_path / "audit.log")
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    target = tmp_path / "doomed.txt"
    target.write_text("bye", encoding="utf-8")

    with patch("aether_engine.workers.sandbox.execute_tool_in_worker",
               new=AsyncMock(return_value="Deleted file")) as worker:
        result = asyncio.run(registry.execute(
            "delete_file",
            {"path": str(target), "api_key": SECRET},
            approved=True, workspace_roots=[str(tmp_path)], audit_logger=audit,
        ))
    assert result == "Deleted file"
    worker.assert_awaited_once()

    events = _events(tmp_path / "audit.log")
    executed = [e for e in events if e.get("action") == "TOOL_EXECUTED"]
    assert len(executed) == 1, f"expected one TOOL_EXECUTED event, got: {events}"
    assert executed[0]["details"]["tool_name"] == "delete_file"
    assert executed[0]["details"]["risk"] == "destructive"
    assert executed[0]["details"]["arg_keys"] == ["api_key", "path"]
    assert SECRET not in (tmp_path / "audit.log").read_text()
    assert str(target) not in (tmp_path / "audit.log").read_text()


def test_read_only_tool_execution_is_not_audited_as_tool_executed(tmp_path):
    """Guard: READ_ONLY executions change nothing, so they must not emit
    TOOL_EXECUTED (otherwise every get_current_time call floods the
    trail)."""
    from aether_engine.tools.registry import create_current_time_tool

    audit = AuditLogger(log_path=tmp_path / "audit.log")
    registry = ToolRegistry()
    registry.register(create_current_time_tool())
    asyncio.run(registry.execute("get_current_time", audit_logger=audit))
    assert "TOOL_EXECUTED" not in _actions(_events(tmp_path / "audit.log"))
