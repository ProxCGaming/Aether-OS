"""Resource-capped launcher for stdio MCP servers (E3-F05).

Every stdio MCP server is spawned THROUGH this file: the MCP client
runs ``[sys.executable, <this file>, --cpu-seconds=N, --memory-mb=M,
--, command, *args]`` and the SDK believes this process is the server.
That spawn seam is the one place the child's resource limits can be
applied — the MCP SDK (1.30) spawns with no limit hooks (POSIX:
``anyio.open_process`` in a new session; Windows: a Job Object with
only ``KILL_ON_JOB_CLOSE``) and portable code cannot set limits on a
process it did not start. Before this, a stdio server inherited the
engine's unlimited CPU/memory limits (verified by the Astra review:
the real child reported unlimited soft/hard limits), while Aether's
own worker-executed tools run under Job Object CPU-time/memory caps.

This file is intentionally SELF-CONTAINED (stdlib only, plus pywin32
on Windows) and is invoked by absolute path, never ``python -m``: it
must run from any working directory — a server's configured ``cwd``
is arbitrary and the engine package is not installed into its own
venv — so importing ``aether_engine`` from here would be fragile.

POSIX: setrlimit, then exec — the server REPLACES this process (same
PID, same process group, stdio inherited), so the SDK's process-group
termination and the session owner's lifecycle (E3-F03) apply to the
server itself, and the limits are the server's own:

- ``RLIMIT_CPU`` (soft = cap, hard = cap + 5s): a cumulative CPU
  budget for the server's lifetime. The kernel sends SIGXCPU at the
  soft limit (default disposition: terminate) and SIGKILL at the
  hard limit, so even a server that catches SIGXCPU is bounded.
- ``RLIMIT_DATA`` (soft = hard = cap): a ceiling on committed data
  memory (heap/brk + anonymous mmap). ``RLIMIT_AS`` was rejected
  empirically: it counts PROT_NONE *reservations*, so V8-cage
  runtimes (Node) fail to start at any proportionate cap, while
  RLIMIT_DATA tolerates multi-GB reservations and still blocks
  committed growth. Stated plainly, the known gap: memory committed
  inside an already-reserved NORESERVE region (how V8 commits cage
  pages) is not charged to RLIMIT_DATA, so for such runtimes the
  POSIX memory cap is partial. The CPU cap is not.

Windows: there is no exec, so the launcher stays as a supervisor. It
creates a Job Object in the same shape as the worker's
(``workers/job_object.py``) — ``JOB_OBJECT_LIMIT_JOB_TIME`` for CPU,
``JOB_OBJECT_LIMIT_JOB_MEMORY`` for committed memory — plus
``KILL_ON_JOB_CLOSE``, spawns the resolved server command with
inherited stdio, assigns it to the job, and waits. When the launcher
exits or is terminated, the job handle closes and the server dies
with it: no orphans. If pywin32 is unavailable or the assignment
fails, the launcher REFUSES (exit 3, reason on stderr) rather than
start a server uncapped — the same fail-closed rule the worker
sandbox applies when its limits cannot be enforced (ADR 0010).

Cap value 0 for a dimension means "no cap for that dimension" — an
explicit per-server config choice by the user, never the default
(the client supplies the defaults when the config is silent).
"""

import os
import sys

_EXIT_REFUSED = 3
_EXIT_NO_SUCH_COMMAND = 127


def _parse(argv: list[str]) -> tuple[int, int, list[str]]:
    """Split ``--cpu-seconds=N --memory-mb=M -- command args...``.

    Manual parsing on purpose: everything after ``--`` is the server's
    own command line and must pass through untouched, including args
    that look like launcher flags.
    """
    cpu_seconds = 0
    memory_mb = 0
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("--cpu-seconds="):
            cpu_seconds = int(arg.partition("=")[2])
        elif arg.startswith("--memory-mb="):
            memory_mb = int(arg.partition("=")[2])
        else:
            break
        i += 1
    return cpu_seconds, memory_mb, argv[i:]


def _run_posix(cpu_seconds: int, memory_mb: int, command: list[str]) -> int:
    import resource

    if cpu_seconds > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
    if memory_mb > 0:
        cap = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as e:
        print(f"aether stdio launcher: cannot start {command[0]!r}: {e}",
              file=sys.stderr, flush=True)
        return _EXIT_NO_SUCH_COMMAND
    return _EXIT_REFUSED  # unreachable: execvpe only returns via exception


def _resolve_windows_command(command: str) -> str:
    """Resolve like the SDK would have for the direct command (npx and
    friends are .cmd shims that CreateProcess cannot start as-is)."""
    try:
        from mcp.os.win32.utilities import get_windows_executable_command

        return get_windows_executable_command(command)
    except Exception:  # noqa: BLE001 — fall back to a plain PATH lookup
        import shutil

        return shutil.which(command) or command


def _run_windows(cpu_seconds: int, memory_mb: int, command: list[str]) -> int:
    import subprocess

    try:
        import win32api
        import win32job
    except ImportError:
        print(
            "aether stdio launcher: pywin32 is unavailable, so resource "
            "limits cannot be enforced; refusing to start the server uncapped",
            file=sys.stderr, flush=True,
        )
        return _EXIT_REFUSED

    job = win32job.CreateJobObject(None, "")
    basic = win32job.QueryInformationJobObject(
        job, win32job.JobObjectBasicLimitInformation
    )
    basic["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if cpu_seconds > 0:
        basic["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_JOB_TIME
        # PerJobUserTimeLimit is in 100-nanosecond ticks (worker parity).
        basic["PerJobUserTimeLimit"] = int(cpu_seconds * 10_000_000)
    win32job.SetInformationJobObject(
        job, win32job.JobObjectBasicLimitInformation, basic
    )
    if memory_mb > 0:
        extended = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        extended["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        extended["JobMemoryLimit"] = int(memory_mb * 1024 * 1024)
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, extended
        )

    resolved = _resolve_windows_command(command[0])
    argv = [resolved, *command[1:]]
    if resolved.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("ComSpec", "cmd.exe"), "/c", *argv]
    # stdio is inherited untouched: protocol on stdout, logs on stderr.
    proc = subprocess.Popen(
        argv,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE — the same rights the
        # worker's assign_process requests (workers/job_object.py).
        handle = win32api.OpenProcess(0x0100 | 0x0001, False, proc.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
    except Exception as e:  # noqa: BLE001 — assignment failure refuses the run (fail closed)
        proc.kill()
        proc.wait()
        print(
            f"aether stdio launcher: could not assign the server to its "
            f"resource-limit job object ({e}); refusing to run it uncapped",
            file=sys.stderr, flush=True,
        )
        return _EXIT_REFUSED
    return proc.wait()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        cpu_seconds, memory_mb, command = _parse(args)
    except ValueError as e:
        print(f"aether stdio launcher: bad limit arguments: {e}",
              file=sys.stderr, flush=True)
        return _EXIT_REFUSED
    if not command:
        print("aether stdio launcher: no server command given",
              file=sys.stderr, flush=True)
        return _EXIT_REFUSED
    if sys.platform == "win32":
        return _run_windows(cpu_seconds, memory_mb, command)
    return _run_posix(cpu_seconds, memory_mb, command)


if __name__ == "__main__":
    raise SystemExit(main())
