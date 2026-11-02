"""Shared sandbox execution: runs risky tools in an isolated aether_worker subprocess.

Extracted from app.py so both the WebSocket handler and the LangGraph execute_tool
node can route risky tool calls through the same sandboxed worker path.

See ADR 0009 (process isolation), ADR 0010 (resource limits), ADR 0011 §2 (preservation).

Spawn contract (EGM S2-M4 / S2-M5):
- The tool payload travels ONLY on the worker's stdin (one JSON
  document, read to EOF by ``aether_worker.__main__``). It is never an
  argv element: argv is readable by any same-user process via ``ps`` /
  ``/proc/<pid>/cmdline`` and is OS-size-bounded.
- The child environment is built from the explicit
  ``WORKER_ENV_ALLOWLIST`` below plus ``AETHER_WORKER_TOKEN`` — never
  ``os.environ`` wholesale, which holds provider API keys.
"""
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from aether_common.auth import read_token
from aether_engine.audit import AuditLogger
from aether_engine.workers.job_object import WorkerJobObject

logger = logging.getLogger("aether_engine.workers.sandbox")

# S2-M5: minimal environment allow-list for the worker child, mirroring
# MCP_STDIO_ENV_ALLOWLIST (aether_engine/mcp/client.py, issue 011 S2) —
# same subprocess-boundary reasoning, defined locally so the workers
# package does not import the MCP client for a constant; a test pins
# the two sets equal so neither can drift silently. The worker itself
# reads only AETHER_WORKER_TOKEN (added separately, never part of this
# set); everything here is for the worker's own runtime and the shell /
# check subprocesses it spawns on the user's behalf:
# - PATH/PATHEXT: locate `python -m` support tools and shell commands.
# - HOME/USER*/LOGNAME + Windows profile dirs: tools resolve the
#   user's home/config/temp locations from these.
# - LANG/LC_*/LANGUAGE: locale + text encoding for Python and tools.
# - TEMP/TMP/TMPDIR: temp files (Python tempfile, compilers, checks).
# - SystemRoot/SystemDrive/WINDIR/ComSpec/ProgramFiles*/OS: Windows
#   system dirs; ComSpec is the shell create_subprocess_shell runs.
# - VIRTUAL_ENV/PYTHONPATH: the venv + module path the worker and
#   run_checks children (pytest etc.) resolve Python code through.
# - SHELL/TERM: POSIX shell selection + terminal behaviour.
# - Proxy / CA-bundle vars: network tools the user approved (git,
#   curl, npm, pip) reach the network and verify TLS through these;
#   endpoints/file paths only in the normal case (credentialed proxy
#   URLs are the separately recorded EGM S2 N2 item, not this fix).
WORKER_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME",
        "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE",
        "TEMP", "TMP", "TMPDIR",
        "SystemRoot", "SystemDrive", "WINDIR", "ComSpec",
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
        "VIRTUAL_ENV", "PYTHONPATH", "SHELL", "TERM", "OS",
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
        "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY",
        "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)


def _build_worker_env(worker_token: str) -> dict[str, str]:
    """Child env for the worker: allowlisted vars only, plus the token.

    Provider API keys and every other engine variable are excluded by
    construction (S2-M5) — a name reaches the child only if it is in
    ``WORKER_ENV_ALLOWLIST``.
    """
    env = {k: v for k, v in os.environ.items() if k in WORKER_ENV_ALLOWLIST}
    env["AETHER_WORKER_TOKEN"] = worker_token
    return env


# Default task-class resource profiles (mirrors UserConfig defaults).
_DEFAULT_TASK_CLASSES: dict[str, dict[str, int]] = {
    "instant": {"time_cap_seconds": 10, "memory_cap_mb": 512},
    "quick": {"time_cap_seconds": 60, "memory_cap_mb": 1024},
    "standard": {"time_cap_seconds": 1800, "memory_cap_mb": 4096},
    "heavy": {"time_cap_seconds": 7200, "memory_cap_mb": 8192},
}


async def execute_tool_in_worker(
    tool_name: str,
    args: dict[str, Any],
    task_class: str,
    audit_logger: AuditLogger | None = None,
    task_classes: dict[str, dict[str, int]] | None = None,
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
        events are recorded in the audit trail. ``WORKER_EXECUTION_TIMEOUT``
        is recorded when the worker is killed for exceeding its profile's
        wall-clock time cap; ``WORKER_EXECUTION_REFUSED`` when the run is
        refused (limits unenforceable, token unavailable, or the worker
        could not be assigned to the Job Object).
    task_classes : dict, optional
        Override mapping of task-class names → ``{time_cap_seconds, memory_cap_mb}``.
        Falls back to built-in defaults when *None*.
    """
    profiles = task_classes or _DEFAULT_TASK_CLASSES
    limits = profiles.get(task_class)
    if limits is None:
        # E2-F05: the selected profile must be the enforced profile.
        # The old fallback silently ran the call under another class's
        # caps — limits the user never selected. An unknown class is a
        # configuration error: refuse loudly, fail closed, in the same
        # shape as every other unenforceable-limits refusal below.
        reason = (
            f"unknown task_class {task_class!r}: no resource profile "
            f"configured under that name (configured: {sorted(profiles)})"
        )
        logger.error(f"Refusing to execute '{tool_name}': {reason}")
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_REFUSED", {
                "tool": tool_name,
                "task_class": task_class,
                "reason": reason,
            })
        raise RuntimeError(f"Refusing to execute '{tool_name}': {reason}")
    time_cap = limits["time_cap_seconds"]
    mem_cap = limits["memory_cap_mb"]

    payload = json.dumps({"tool_name": tool_name, "args": args})

    job_obj = None
    try:
        job_obj = WorkerJobObject(time_cap, mem_cap)
    except Exception as e:  # noqa: BLE001 — any job-object failure refuses the run (fail closed, see below)
        # Fail closed (Phase 9, A3 / ADR 0010): if resource limits cannot be
        # enforced, the tool must NOT run. The old behavior logged a warning
        # and continued with job_obj=None, i.e. silently unlimited execution.
        reason = f"resource limits unenforceable: {e}"
        logger.error(f"Refusing to execute '{tool_name}': {reason}")
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_REFUSED", {
                "tool": tool_name,
                "task_class": task_class,
                "reason": reason,
            })
        raise RuntimeError(f"Refusing to execute '{tool_name}': {reason}")

    start_time = time.time()

    # Phase 10.2, A1 (Option 2): the worker only accepts payloads
    # authenticated with the per-session engine token (ADR 0003). Read it
    # here and hand it to the child via the environment — never argv, so
    # it does not appear in `ps` output. If the token is unavailable the
    # tool must NOT run (fail closed, same pattern as the job-object
    # refusal above).
    try:
        worker_token = read_token()
    except (FileNotFoundError, ValueError) as e:
        reason = f"engine token unavailable: {e}"
        logger.error(f"Refusing to execute '{tool_name}': {reason}")
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_REFUSED", {
                "tool": tool_name,
                "task_class": task_class,
                "reason": reason,
            })
        raise RuntimeError(f"Refusing to execute '{tool_name}': {reason}")

    # S2-M4: payload goes over stdin, never argv (argv is visible in
    # `ps` / /proc/<pid>/cmdline and size-bounded). S2-M5: env is the
    # allowlist + token, never the engine's full environment.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aether_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_build_worker_env(worker_token),
    )

    # Phase 10.2, A2: if the worker cannot be assigned to the Job Object,
    # its time/memory limits are unenforceable — refuse the execution
    # (fail closed, same as a Job Object construction failure above).
    # The old code swallowed this with `except Exception: pass` and the
    # worker then ran with NO limits. The just-spawned process is killed
    # before raising so a refused run leaves no orphan behind.
    # Note: assignment happens immediately after spawn, so the window in
    # which the worker could act unassigned is minimal but not zero —
    # true pre-spawn assignment (CREATE_SUSPENDED) is a separate change.
    if job_obj and proc.pid:
        try:
            job_obj.assign_process(proc.pid)
        except Exception as e:  # noqa: BLE001 — assignment failure refuses the run (fail closed)
            reason = f"failed to assign worker to Job Object: {e}"
            logger.error(f"Refusing to execute '{tool_name}': {reason}")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.communicate()
            except Exception:  # noqa: BLE001, S110 — best-effort reap of an already-killed worker during cleanup
                pass
            if audit_logger:
                audit_logger.log_event("WORKER_EXECUTION_REFUSED", {
                    "tool": tool_name,
                    "task_class": task_class,
                    "reason": reason,
                })
            raise RuntimeError(f"Refusing to execute '{tool_name}': {reason}")

    try:
        # Phase 10.2, A2: wall-clock timeout. The Job Object's
        # PerJobUserTimeLimit is CPU user-time only, so a sleeping /
        # network-waiting worker never trips it. The bound is the time
        # cap of the profile actually in use for this call (instant 10s /
        # quick 60s / standard 1800s / heavy 7200s, or the caller's
        # task_classes override). On timeout the worker is killed and a
        # distinct WORKER_EXECUTION_TIMEOUT audit event is recorded.
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=payload.encode("utf-8")), timeout=time_cap
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001, S110 — best-effort reap of an already-killed worker during cleanup
            pass
        wall_clock = time.time() - start_time
        reason = f"worker exceeded {time_cap}s wall-clock cap (task_class={task_class})"
        logger.error(f"Worker timeout for '{tool_name}': {reason}")
        if audit_logger:
            audit_logger.log_event("WORKER_EXECUTION_TIMEOUT", {
                "tool": tool_name,
                "task_class": task_class,
                "time_cap_seconds": time_cap,
                "reason": reason,
                "wall_clock_seconds": round(wall_clock, 2),
            })
        raise RuntimeError(f"Worker timed out after {time_cap}s (task_class={task_class}): killed")
    except Exception as e:  # noqa: BLE001 — worker failures are reported via the audit log and a result dict, never raised
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
