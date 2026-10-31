import json
import re
import time
from pathlib import Path
from typing import Any

from aether_common.contracts import TaskState

DEFAULT_AUDIT_LOG_PATH = Path("logs/audit.log")

# Phase 10.2, C2: the audit log is plaintext and world-readable by default
# (critical/07 M2/M5), so no secret value and no full tool payload may ever
# reach it. Redaction is centralised here, in _append — every log_* method
# funnels through it, so callers that still pass full args/payloads (e.g.
# websocket.py APPROVAL_REQUESTED / TASK_TRANSITION) are sanitised without
# touching each call site. This is NOT a rebuild of the audit log: no
# hashing, rotation, fsync, locking or sequencing is added here — that is a
# follow-up (critical/07 M3); see the C2 report.
_REDACTED = "[REDACTED]"

# Dict keys whose *value* is a secret (matched on the normalised key:
# lower-cased, '-' -> '_'). "author" must NOT match: bare "auth" is not a
# substring here, only token-bearing forms are.
_SENSITIVE_KEY_PARTS = (
    "api_key", "apikey", "secret", "token", "password", "passwd",
    "credential", "private_key", "access_key", "bearer", "session_key",
)

# Dict keys whose value is a payload, not metadata: never log the value,
# only the shape (which fields were present). "content" (file contents)
# and "command" (shell text) are dropped entirely — even a shape summary
# of file contents is meaningless and a command can embed a secret.
_PAYLOAD_KEYS = frozenset({"args", "payload", "manifest"})

# Bare-string secret patterns, for a secret passed as a plain value rather
# than under a sensitive key (e.g. inside a list). Deliberately narrow:
# well-known key prefixes only, so ordinary log text is untouched.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _is_sensitive_key(key: str) -> bool:
    norm = str(key).lower().replace("-", "_")
    if norm == "key" or norm.endswith("_key"):
        return True
    return any(part in norm for part in _SENSITIVE_KEY_PARTS)


def _scrub_string(value: str) -> str:
    scrubbed = value
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(_REDACTED, scrubbed)
    return scrubbed


def _redact(value: Any, key: str | None = None) -> Any:
    """Return a log-safe copy of *value*.

    - A value under a sensitive key, or under "content"/"command", becomes
      "[REDACTED]" — the key value (api_key, token, file content, shell
      command) never reaches the log.
    - A value under a payload key ("args"/"payload"/"manifest") becomes
      {"keys": [...]} — the caller learns which fields were present, never
      their values, so a full tool payload is never logged.
    - Anything else is recursed into (dicts/lists) or pattern-scrubbed
      (strings). Non-string scalars pass through unchanged.
    """
    if isinstance(value, dict):
        if key in _PAYLOAD_KEYS:
            return {"keys": sorted(str(k) for k in value)}
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        if key is not None and (_is_sensitive_key(key) or key in ("content", "command")):
            return _REDACTED
        if key in _PAYLOAD_KEYS:
            return _REDACTED
        return _scrub_string(value)
    return value


class AuditLogger:
    def __init__(self, log_path: Path = DEFAULT_AUDIT_LOG_PATH,
                 max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Epic 10, E10-02: size-based rotation (the follow-up the C2
        # note above deferred). Hash-chaining stays an accepted,
        # documented risk — see ADR 0037.
        self.max_bytes = max_bytes
        self.backup_count = backup_count

    def _archive_path(self, index: int) -> Path:
        return self.log_path.with_name(f"{self.log_path.name}.{index}.gz")

    def _rotate(self) -> None:
        import gzip
        import shutil

        for i in range(self.backup_count - 1, 0, -1):
            src = self._archive_path(i)
            if src.exists():
                src.replace(self._archive_path(i + 1))
        with open(self.log_path, "rb") as f_in, gzip.open(self._archive_path(1), "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        self.log_path.write_text("", encoding="utf-8")

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(_redact(record)) + "\n"
        rotated = False
        try:
            if (
                self.max_bytes
                and self.log_path.exists()
                and self.log_path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes
            ):
                self._rotate()
                rotated = True
        except Exception:  # noqa: BLE001 — rotation is housekeeping: a rotation failure must never lose the audit write itself
            rotated = False
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)
        if rotated:
            self.log_event("AUDIT_LOG_ROTATED", {
                "max_bytes": self.max_bytes,
                "backup_count": self.backup_count,
                "archive": self._archive_path(1).name,
            })

    def log_connection(self, success: bool, ip: str = "unknown",
                       port: int | None = None, reason: str = "") -> None:
        self._append({"type": "CONNECTION_ATTEMPT", "ts": time.time(),
                       "success": success, "client_ip": ip,
                       "client_port": port, "reason": reason})

    def log_task_transition(self, task_id: str, from_st: TaskState,
                            to_st: TaskState, payload: dict | None = None) -> None:
        self._append({"type": "TASK_TRANSITION", "ts": time.time(),
                       "task_id": task_id,
                       "from_state": from_st.value, "to_state": to_st.value,
                       "payload": payload or {}})

    def log_event(self, action: str, details: dict | None = None) -> None:
        self._append({"type": "ENGINE_EVENT", "ts": time.time(),
                       "action": action, "details": details or {}})

    def log_validation(self, tool_name: str, valid: bool, message: str, payload: dict | None = None) -> None:
        self._append({"type": "VALIDATION", "ts": time.time(),
                      "tool_name": tool_name, "valid": valid, "message": message, "payload": payload or {}})
