"""Heuristic task class inference for Aether OS resource profiles."""

import re
from typing import Any, Dict


def infer_task_class(tool_name: str, args: Dict[str, Any]) -> str:
    """Infer the task class (instant, quick, standard, heavy, custom) based on tool name and arguments.

    Heuristic Rules:
    - `delete_file` -> `instant`
    - `write_file` -> `quick`
    - `execute_shell` -> inspect command: if compiling (gcc, cargo build, etc.) or heavy data processing -> `heavy`; otherwise -> `standard`.
    - Unrecognized tools -> `standard`
    """
    if tool_name == "delete_file":
        return "instant"

    if tool_name == "write_file":
        return "quick"

    if tool_name == "execute_shell":
        command = str(args.get("command", "")).strip().lower()
        
        # Heavy computation, build, or data processing patterns
        heavy_patterns = [
            r"\bgcc\b",
            r"\bg\+\+\b",
            r"\bcargo\s+(build|run|test|check)\b",
            r"\bnpm\s+(run\s+build|install)\b",
            r"\byarn\s+(build|install)\b",
            r"\bmsbuild\b",
            r"\bcmake\b",
            r"\bmake\b",
            r"\bdocker\s+(build|run)\b",
            r"\bpython\s+[\w\/\\]*\.py\b" # running arbitrary python scripts could be heavy
        ]
        
        for pat in heavy_patterns:
            if re.search(pat, command):
                return "heavy"
                
        return "standard"

    return "standard"
