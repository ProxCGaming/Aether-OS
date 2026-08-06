import json, time
from pathlib import Path
from typing import Any, Dict, Optional
from aether_common.contracts import TaskState

DEFAULT_AUDIT_LOG_PATH = Path("logs/audit.log")


class AuditLogger:
    def __init__(self, log_path: Path = DEFAULT_AUDIT_LOG_PATH):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, record: Dict[str, Any]) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def log_connection(self, success: bool, ip: str = "unknown",
                       port: Optional[int] = None, reason: str = "") -> None:
        self._append({"type": "CONNECTION_ATTEMPT", "ts": time.time(),
                       "success": success, "client_ip": ip,
                       "client_port": port, "reason": reason})

    def log_task_transition(self, task_id: str, from_st: TaskState,
                            to_st: TaskState, payload: Optional[Dict] = None) -> None:
        self._append({"type": "TASK_TRANSITION", "ts": time.time(),
                       "task_id": task_id,
                       "from_state": from_st.value, "to_state": to_st.value,
                       "payload": payload or {}})

    def log_event(self, action: str, details: Optional[Dict] = None) -> None:
        self._append({"type": "ENGINE_EVENT", "ts": time.time(),
                       "action": action, "details": details or {}})

    def log_validation(self, tool_name: str, valid: bool, message: str, payload: Optional[Dict] = None) -> None:
        self._append({"type": "VALIDATION", "ts": time.time(),
                      "tool_name": tool_name, "valid": valid, "message": message, "payload": payload or {}})
