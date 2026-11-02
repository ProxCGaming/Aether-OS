import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _is_path_in_roots(target_path: Path, roots: list[Path]) -> bool:
    try:
        resolved_target = target_path.resolve()
        return any(resolved_target.is_relative_to(root.resolve()) for root in roots)
    except Exception:  # noqa: BLE001 — fail-closed boundary: any resolution error means "not contained", never an exception escaping a security check.
        return False
@dataclass
class ValidationResult:
    is_valid: bool
    error: str | None = None
    details: dict[str, Any] | None = None


def validate_tool_request(
    tool_name: str, 
    args: dict[str, Any] | None = None,
    workspace_roots: list[str] | None = None
) -> ValidationResult:
    args = args or {}
    
    roots = []
    if workspace_roots:
        roots = [Path(r) for r in workspace_roots]
    else:
        roots = [Path.home() / "AetherWorkspace"]


    if tool_name == "read_file":
        # Epic 2, E2-01: reads obey the same containment as writes. The
        # tool enforces it again in-process (defence in depth); this
        # branch keeps the pipeline's verdict identical to the tool's.
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Read validation failed: path '{path}' is outside permitted workspace roots.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name in {"find_files", "search_text"}:
        # Epic 2, E2-02: find/search obey the same containment for their
        # optional `path` arg (a file or directory); pattern/query-only
        # calls search the roots themselves and have nothing to check.
        # The tools enforce containment again in-process.
        raw_path = args.get("path")
        if raw_path:
            path = Path(raw_path)
            if not _is_path_in_roots(path, roots):
                return ValidationResult(False, f"Search validation failed: path '{path}' is outside permitted workspace roots.")
            return ValidationResult(True, details={"path": str(path)})
        return ValidationResult(True, details={"tool_name": tool_name})

    if tool_name == "write_file":
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Write validation failed: path '{path}' is outside permitted workspace roots.")
        # Epic 2, E2-04 (D3): missing parents are created by the tool, so
        # writability is judged at the nearest EXISTING ancestor — which
        # containment already proved is inside a root.
        ancestor = path.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not os.access(ancestor, os.W_OK):
            return ValidationResult(False, "Write validation failed: parent directory is not writable.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name == "edit_file":
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Edit validation failed: path '{path}' is outside permitted workspace roots.")
        if not path.is_file():
            return ValidationResult(False, "Edit validation failed: target file does not exist.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name == "create_directory":
        path = Path(args.get("path", ""))
        if not _is_path_in_roots(path, roots):
            return ValidationResult(False, f"Create-directory validation failed: path '{path}' is outside permitted workspace roots.")
        return ValidationResult(True, details={"path": str(path)})

    if tool_name in ("project_tree", "run_checks"):
        # Epic 2, E2-11: both take a directory inside the roots
        # (project_tree's path is optional — absent means the root).
        raw = args.get("path")
        if raw:
            path = Path(str(raw))
            if not _is_path_in_roots(path, roots):
                return ValidationResult(False, f"Path validation failed: path '{path}' is outside permitted workspace roots.")
            return ValidationResult(True, details={"path": str(path)})
        return ValidationResult(True, details={"tool_name": tool_name})

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
        # Epic 2, E2-07: segment-wise safety rules run before anything
        # else — a deny verdict fails validation outright (the policy
        # layer independently denies too; this is defence in depth for
        # paths that reach execution without a fresh policy check).
        from aether_engine.validation.shell_rules import evaluate_shell_command

        shell_verdict = evaluate_shell_command(command)
        if shell_verdict.verdict == "deny":
            return ValidationResult(False, f"Shell validation failed: command blocked by safety rule '{shell_verdict.rule}'.")
        executable = command.split()[0]
        if executable in {"echo", "cd", "pwd", "ls", "dir"}:
            return ValidationResult(True, details={"executable": executable, "builtin": True})
        resolved = shutil.which(executable)
        if not resolved:
            return ValidationResult(False, f"Shell validation failed: executable '{executable}' not found in PATH.")
        return ValidationResult(True, details={"executable": resolved, "builtin": False})

    return ValidationResult(True, details={"tool_name": tool_name})
