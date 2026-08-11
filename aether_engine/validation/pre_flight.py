import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


def _is_path_in_roots(target_path: Path, roots: List[Path]) -> bool:
    try:
        resolved_target = target_path.resolve()
        return any(resolved_target.is_relative_to(root.resolve()) for root in roots)
    except Exception:
        return False
@dataclass
class ValidationResult:
    is_valid: bool
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


def validate_tool_request(
    tool_name: str, 
    args: Optional[Dict[str, Any]] = None,
    workspace_roots: Optional[List[str]] = None
) -> ValidationResult:
    args = args or {}
    
    roots = []
    if workspace_roots:
        roots = [Path(r) for r in workspace_roots]
    else:
        roots = [Path.home() / "AetherWorkspace"]


    if tool_name == "write_file":
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Write validation failed: path '{path}' is outside permitted workspace roots.")
        if not path.parent.exists():
            return ValidationResult(False, "Write validation failed: parent directory does not exist.")
        if not os.access(path.parent, os.W_OK):
            return ValidationResult(False, "Write validation failed: parent directory is not writable.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name == "delete_file":
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Delete validation failed: path '{path}' is outside permitted workspace roots.")
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
