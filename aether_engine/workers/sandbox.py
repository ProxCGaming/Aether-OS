"""Shared sandbox execution: runs risky tools in an isolated aether_worker subprocess.

Extracted from app.py so both the WebSocket handler and the LangGraph execute_tool
node can route risky tool calls through the same sandboxed worker path.

See ADR 0009 (process isolation), ADR 0010 (resource limits), ADR 0011 §2 (preservation).
"""
import asyncio
import json
import logging
import sys
import time
from typing import Any, Dict, Optional

from aether_engine.audit import AuditLogger
from aether_engine.workers.job_object import WorkerJobObject

logger = logging.getLogger("aether_engine.workers.sandbox")

# Default task-class resource profiles (mirrors UserConfig defaults).
_DEFAULT_TASK_CLASSES: Dict[str, Dict[str, int]] = {
    "instant": {"time_cap_seconds": 10, "memory_cap_mb": 512},
    "quick": {"time_cap_seconds": 60, "memory_cap_mb": 1024},
    "standard": {"time_cap_seconds": 1800, "memory_cap_mb": 4096},
    "heavy": {"time_cap_seconds": 7200, "memory_cap_mb": 8192},
}


async def execute_tool_in_worker(
    tool_name: str,
    args: Dict[str, Any],
    task_class: str,
    audit_logger: Optional[AuditLogger] = None,
    task_classes: Optional[Dict[str, Dict[str, int]]] = None,
) -> Any:
    """Run an approved risky tool in a dedicated worker subprocess and return its result.

    Parameters
    ----------
    tool_name : str
        Registry name of the tool (e.g. ``write_file``).
    args : dict
        Arguments forwarded to the worker.
    task_class : str
        Resource-limit profile name (``instant``, ``quick``, ``standard``, ``heavy``).
    audit_logger : AuditLogger, optional
        If provided, ``WORKER_EXECUTION_COMPLETED`` / ``WORKER_EXECUTION_TERMINATED``
        events are recorded in the audit trail.
    task_classes : dict, optional
        Override mapping of task-class names → ``{time_cap_seconds, memory_cap_mb}``.
        Falls back to built-in defaults when *None*.
    """
    profiles = task_classes or _DEFAULT_TASK_CLASSES
    limits = profiles.get(task_class, profiles.get("standard", {"time_cap_seconds": 1800, "memory_cap_mb": 4096}))
    time_cap = limits["time_cap_seconds"]
    mem_cap = limits["memory_cap_mb"]

    payload = json.dumps({"tool_name": tool_name, "args": args})

    job_obj = None
    try:
        job_obj = WorkerJobObject(time_cap, mem_cap)
    except Exception as e:
        logger.warning(f"Failed to create Job Object: {e}")

    start_time = time.time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aether_worker",
        "--payload",
        payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if job_obj and proc.pid:
        try:
            job_obj.assign_process(proc.pid)
        except Exception:
            pass

    try:
        stdout, stderr = await proc.communicate()
    except Exception as e:
        logger.error(f"Worker process error: {e}")
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_TERMINATED", {
                "tool": tool_name,
                "task_class": task_class,
                "reason": str(e),
                "wall_clock_seconds": round(time.time() - start_time, 2),
            })
        raise RuntimeError(f"Worker terminated: {e}")

    wall_clock = time.time() - start_time
    if proc.returncode != 0:
        err_msg = (
            stderr.decode(errors="ignore")
            or stdout.decode(errors="ignore")
            or f"worker failed (code {proc.returncode})"
        )
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_TERMINATED", {
                "tool": tool_name,
                "task_class": task_class,
                "exit_code": proc.returncode,
                "reason": "Limit breach or internal error",
                "wall_clock_seconds": round(wall_clock, 2),
            })
        raise RuntimeError(err_msg)

    if audit_logger:
        audit_logger.log_event("WORKER_EXECUTION_COMPLETED", {
            "tool": tool_name,
            "task_class": task_class,
            "wall_clock_seconds": round(wall_clock, 2),
        })

    text = stdout.decode(errors="ignore").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed
