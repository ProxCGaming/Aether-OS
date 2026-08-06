import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ValidationResult:
    is_valid: bool
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def validate_tool_request(tool_name: str, args: Optional[Dict[str, Any]] = None) -> ValidationResult:
    args = args or {}

    if tool_name == "write_file":
        path = Path(args.get("path", ""))
        if not path.parent.exists():
            return ValidationResult(False, "Write validation failed: parent directory does not exist.")
        if not os.access(path.parent, os.W_OK):
            return ValidationResult(False, "Write validation failed: parent directory is not writable.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name == "delete_file":
        path = Path(args.get("path", ""))
        if not path.exists():
            return ValidationResult(False, "Delete validation failed: target path does not exist.")
        if not os.access(path.parent, os.W_OK):
            return ValidationResult(False, "Delete validation failed: parent directory is not writable.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name == "execute_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            return ValidationResult(False, "Shell validation failed: empty command.")
        executable = command.split()[0]
        if executable in {"echo", "cd", "pwd", "ls", "dir"}:
            return ValidationResult(True, details={"executable": executable, "builtin": True})
        resolved = shutil.which(executable)
        if not resolved:
            return ValidationResult(False, f"Shell validation failed: executable '{executable}' not found in PATH.")
        return ValidationResult(True, details={"executable": resolved, "builtin": False})

    return ValidationResult(True, details={"tool_name": tool_name})
