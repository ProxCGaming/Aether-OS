import itertools
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aether_common.contracts import TaskState

logger = logging.getLogger("aether_engine.audit")

# S1-N1: the audit trail lives at one canonical location under
# ~/.aether (the README rule: everything lives in ~/.aether). The old
# default was the CWD-relative Path("logs/audit.log"), so where the
# trail landed depended on each process's launch directory — the
# repo-root dev launch and the desktop-spawned engine wrote different
# logs, and constructing a logger mkdir'd a logs/ dir in whatever cwd
# imported it. The worker subprocess uses this same path
# (aether_worker/__main__.py).
DEFAULT_AUDIT_LOG_PATH = Path.home() / ".aether" / "logs" / "audit.log"

# Many AuditLogger instances exist in one engine process (module-level
# loggers in memory/* and the supervisor, per-call constructions in
# providers/MCP). Serialise rotate+append across all of them so
# concurrent writers in this process cannot interleave lines or race
# a rotation. Cross-process appends (the worker) are single-line
# O_APPEND writes and stay best-effort, as before.
_APPEND_LOCK = threading.Lock()

# N-33: rotation compression runs on ONE background worker thread,
# never on the caller's stack. Rotating used to gzip up to
# max_bytes synchronously inside the append — on an async handler's
# event loop, and under _APPEND_LOCK, so one rotation stalled every
# audit writer in the process behind a 10 MB compression. A rotation
# now costs the caller a single rename (see AuditLogger._rotate);
# this worker performs the archive shift + gzip afterwards. Jobs
# run strictly FIFO, so each job's shifting sees exactly the
# archives the previous jobs left — the same end state the
# synchronous rotation produced.
_ROTATION_QUEUE: queue.Queue[Callable[[], None]] = queue.Queue()
_ROTATION_SEQ = itertools.count()
_rotation_worker_lock = threading.Lock()
_rotation_worker_started = False


def _rotation_worker() -> None:
    while True:
        job = _ROTATION_QUEUE.get()
        try:
            job()
        except Exception:
            logger.exception("Audit log rotation compression failed")
        finally:
            _ROTATION_QUEUE.task_done()


def _enqueue_rotation(job: Callable[[], None]) -> None:
    global _rotation_worker_started
    with _rotation_worker_lock:
        if not _rotation_worker_started:
            _rotation_worker_started = True
            threading.Thread(
                target=_rotation_worker, daemon=True,
                name="audit-log-rotation",
            ).start()
    _ROTATION_QUEUE.put(job)


def flush_pending_rotations(timeout: float | None = None) -> bool:
    """Wait until every queued rotation compression has finished.

    Returns True when the queue drained, False on timeout. For
    shutdown and tests — production appends never wait on
    compression (N-33).
    """
    if timeout is None:
        _ROTATION_QUEUE.join()
        return True
    drained = threading.Event()

    def _wait() -> None:
        _ROTATION_QUEUE.join()
        drained.set()

    threading.Thread(target=_wait, daemon=True).start()
    return drained.wait(timeout)

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
    # A whole PEM block first (header + body + footer), so a pasted
    # private key loses its body too; the header-only pattern below
    # remains for a header that appears on its own.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    # E8-12: Google API keys (AIza + 35) — the default provider's
    # key shape, previously missing entirely.
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # AWS access key ids: long-term (AKIA) and temporary (ASIA).
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # GitHub tokens: ghp_ (personal), gho_ (OAuth), ghu_/ghs_
    # (GitHub Apps), ghr_ (refresh), plus the fine-grained PAT form.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# E8-12: credentials embedded in a URL's userinfo
# (scheme://user:password@host). Only the password segment is
# scrubbed, so the URL stays readable in logs and bundles.
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@:]+:)[^\s/@]+@")


def _is_sensitive_key(key: str) -> bool:
    norm = str(key).lower().replace("-", "_")
    if norm == "key" or norm.endswith("_key"):
        return True
    return any(part in norm for part in _SENSITIVE_KEY_PARTS)


def _scrub_string(value: str) -> str:
    scrubbed = _URL_CREDENTIAL_PATTERN.sub(rf"\1{_REDACTED}@", value)
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
        """Stage the live log for background compression (N-33).

        The caller pays one rename: the live file moves to a unique
        staging name (the next append recreates the live file), and
        the rotation worker performs the archive shift + gzip in
        FIFO order. If the process dies before the worker runs, the
        staged file remains next to the log as plain text —
        recoverable, never silently lost.
        """
        staging = self.log_path.with_name(
            f"{self.log_path.name}.rot-{next(_ROTATION_SEQ)}")
        try:
            self.log_path.replace(staging)
        except FileNotFoundError:
            return
        archive_path = self._archive_path
        backup_count = self.backup_count

        def _compress() -> None:
            import gzip
            import shutil

            for i in range(backup_count - 1, 0, -1):
                src = archive_path(i)
                if src.exists():
                    src.replace(archive_path(i + 1))
            with open(staging, "rb") as f_in, \
                    gzip.open(archive_path(1), "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            staging.unlink()

        _enqueue_rotation(_compress)

    def _append_locked(self, line: str) -> bool:
        """Rotate (when due) and append one prepared line.

        Caller must hold _APPEND_LOCK. Returns True when a rotation
        happened, so the caller can record it after releasing the lock
        (the AUDIT_LOG_ROTATED event itself goes through _append).
        """
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
        return rotated

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(_redact(record)) + "\n"
        with _APPEND_LOCK:
            rotated = self._append_locked(line)
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
