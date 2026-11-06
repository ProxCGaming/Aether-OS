"""aether_worker entrypoint: executes one risky tool call in a subprocess.

Trust boundary (Phase 10.2, A1 — ADR 0003 threat model: other local
processes / malicious scripts running as the same user are in scope):
the worker accepts payloads ONLY over an authenticated channel. Before
any dispatch it verifies the per-session engine token — the same token
the engine uses for ``/ws/tasks`` — supplied by the engine via the
``AETHER_WORKER_TOKEN`` environment variable and compared in constant
time against ``.run/engine.token``.

Why token auth (Option 2) instead of re-enforcing policy/pre-flight/
workspace inside the worker (Option 1): the worker has no access to
the user's policies, workspace roots, or approval state, so the engine
would have to pass ``approved``/policy/roots inside the argv payload —
and any direct invoker could set those same plaintext fields in their
own payload, making the "approval" self-asserted and forgeable. Option 1
therefore cannot distinguish an engine-approved call from an attacker's,
and its shell pre-flight is only an existence check anyway. The token
is passed via the environment, never argv, so it does not appear in
``ps`` output (cf. the argv-leak note in the Phase 10 audit).

Payload channel (EGM S2-M4): the payload arrives ONLY on stdin — the
engine writes one JSON document and closes the pipe, and this module
reads stdin to EOF. There is deliberately no ``--payload`` argv form:
argv is readable by any same-user process via ``ps`` /
``/proc/<pid>/cmdline``, so shell commands and file bodies in the
payload would leak, and argv is OS-size-bounded besides. The engine
likewise spawns this worker with an allowlisted environment (S2-M5),
not its full ``os.environ``.

What this does NOT protect against: a same-user process that can read
``.run/engine.token`` can present the token — the same trust basis as
the ``/ws/tasks`` channel itself. True privilege separation (running
the worker as a different/low-privilege user) is out of scope here.

Refusals are fail-closed: unauthenticated, unknown-tool, and malformed
payloads exit non-zero, execute nothing, and are recorded in the audit
log (``WORKER_AUTH_REFUSED`` / ``WORKER_EXECUTION_REFUSED``). No token
value or tool argument is ever written to the audit log.

Note on the audit call: the worker deliberately does NOT import
``aether_engine.audit.AuditLogger`` — importing the ``aether_engine``
package runs its ``__init__`` which imports the whole FastAPI app
(and LiteLLM, which attempts a remote fetch on import), adding ~6s
and a network attempt to every worker spawn. The worker therefore
appends the audit JSONL record directly, in the exact record shape
``AuditLogger.log_event`` produces; if that shape changes, update both.
"""
import asyncio
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from aether_common.auth import read_token, verify_token

WORKER_TOKEN_ENV = "AETHER_WORKER_TOKEN"

# Same canonical path and record shape as aether_engine.audit.AuditLogger:
# under ~/.aether, independent of this process's CWD (S1-N1 — the old
# CWD-relative "logs/audit.log" scattered the trail by launch directory).
DEFAULT_AUDIT_LOG_PATH = Path.home() / ".aether" / "logs" / "audit.log"

# Tools the worker is allowed to execute. Anything else is refused —
# an unknown tool must never report a false success (the old code
# returned {"status": "noop"} for unknown tools).
ALLOWED_TOOLS = frozenset({"execute_shell", "write_file", "delete_file", "edit_file", "create_directory", "run_checks"})

# E9-W3: bounds for the pre-auth stdin read in main(). The payload is
# one tool call's JSON (file bodies included); 16 MiB is generous
# headroom over any legitimate call, while the byte cap and the read
# deadline keep a hostile or broken writer from swelling this process
# without limit or stalling it forever on a held-open pipe.
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
PAYLOAD_READ_TIMEOUT_S = 30.0

# E9-W2: bounds for execute_shell. The parent sandbox bounds the
# worker's WALL-CLOCK (profile time caps) but nothing bounded BYTES:
# communicate() buffered the child's whole output in worker memory,
# returned it as one JSON document, and the parent buffered it again.
# The pumps below keep at most SHELL_OUTPUT_MAX_BYTES per stream —
# the same order as the registry's max_output_chars truncation on
# the in-process path — while draining the rest so the child never
# blocks on a full pipe. SHELL_TIMEOUT_S is the in-worker backstop
# (run_checks uses the same 180s via CHECK_TIMEOUT_S) for direct
# invocations the parent never supervises.
SHELL_OUTPUT_MAX_BYTES = 20_000
SHELL_TIMEOUT_S = 180


async def _pump_capped(stream, out: dict) -> None:
    """Drain *stream*, keeping the first SHELL_OUTPUT_MAX_BYTES.

    Progress is published into *out* per chunk, so a timeout still
    reports whatever was collected before the kill.
    """
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        room = SHELL_OUTPUT_MAX_BYTES - len(buf)
        if room > 0:
            buf += chunk[:room]
            truncated = truncated or len(chunk) > room
        else:
            truncated = True
        out["data"] = bytes(buf)
        out["truncated"] = truncated
    out["data"] = bytes(buf)
    out["truncated"] = truncated


async def _collect_shell_output(proc) -> tuple[str, str, bool]:
    """Collect a shell child's stdout/stderr under the E9-W2 bounds.

    Returns (stdout, stderr, timed_out). On timeout the child is
    killed and the partial output comes back with timed_out=True —
    an execution outcome reported in-band (the run_checks precedent
    in this module), not a refusal.

    The collection is RACED with asyncio.wait, never cancelled:
    cancelling a task that awaits proc.wait() poisons the
    transport's exit waiter (Python 3.12), and the reap after a kill
    then stalls until the command's natural end. On timeout the
    child's whole process GROUP is killed (execute_shell spawns it
    in its own session): killing only the shell would leave a
    grandchild holding the pipes open, and the pumps would see no
    EOF until it exited on its own — the deadline would not bind.
    """
    out_box: dict = {"data": b"", "truncated": False}
    err_box: dict = {"data": b"", "truncated": False}
    collect = asyncio.ensure_future(asyncio.gather(
        _pump_capped(proc.stdout, out_box),
        _pump_capped(proc.stderr, err_box),
        proc.wait(),
    ))
    done, _pending = await asyncio.wait({collect}, timeout=SHELL_TIMEOUT_S)
    timed_out = collect not in done
    if timed_out:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(collect), timeout=5)
        except TimeoutError:
            # Something still holds the pipes open past the group
            # kill — abandon the collection, report what was gathered.
            collect.cancel()
    elif collect.exception() is not None:
        raise collect.exception()
    stdout = out_box["data"].decode(errors="ignore")
    if out_box["truncated"]:
        stdout += f"\n[worker: stdout truncated at {SHELL_OUTPUT_MAX_BYTES} bytes]"
    stderr = err_box["data"].decode(errors="ignore")
    if err_box["truncated"]:
        stderr += f"\n[worker: stderr truncated at {SHELL_OUTPUT_MAX_BYTES} bytes]"
    if timed_out:
        stderr += f"\n[worker: command timed out after {SHELL_TIMEOUT_S}s and was killed]"
    return stdout, stderr, timed_out


async def run_worker(task_payload: dict):
    tool_name = task_payload.get("tool_name")
    args = task_payload.get("args", {})

    if tool_name not in ALLOWED_TOOLS:
        raise ValueError(f"Unknown tool: {tool_name!r}")

    if tool_name == "execute_shell":
        command = args.get("command")
        if not command:
            raise ValueError("Shell command is required")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own session/process group, so the E9-W2 timeout can
            # kill the whole tree (see _collect_shell_output).
            start_new_session=True,
        )
        stdout, stderr, timed_out = await _collect_shell_output(proc)
        result = {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
        if timed_out:
            result["timed_out"] = True
        return result

    if tool_name == "write_file":
        path = args.get("path")
        content = args.get("content", "")
        if not path:
            raise ValueError("File path is required")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)  # E2-04/D3: implicit parents
        target.write_text(content, encoding="utf-8")
        return {"path": path, "written": True}

    if tool_name == "delete_file":
        path = args.get("path")
        if not path:
            raise ValueError("File path is required")
        # E9-W4: report what actually happened — deleting a missing
        # path is a successful no-op (deleted=False), not a deletion.
        # The try/except form (not exists()-then-unlink) also reports
        # a dangling symlink truthfully: the link itself is removed.
        try:
            Path(path).unlink()
            deleted = True
        except FileNotFoundError:
            deleted = False
        return {"path": path, "deleted": deleted}

    if tool_name == "edit_file":
        # Epic 2, E2-03: same aether_common contract the engine's
        # in-process implementation uses — hash check, unique anchors,
        # all-or-nothing, atomic write. Failures raise, so the worker
        # reports a real error and nothing is written.
        from aether_common.text_edit import (
            apply_text_edits,
            atomic_write_bytes,
            content_hash,
        )

        path = args.get("path")
        edits = args.get("edits") or []
        # E2-F01: keep "not supplied" (absent/None) distinct from a
        # supplied value — a supplied empty string is a hash that can
        # never match, not a request to skip the check below.
        expected_hash = args.get("expected_hash")
        if not path:
            raise ValueError("File path is required")
        target = Path(path)
        if not target.is_file():
            raise ValueError(f"edit_file failed: '{path}' does not exist or is not a file.")
        data = target.read_bytes()
        actual = content_hash(data)
        if expected_hash is not None and actual != expected_hash:
            raise ValueError(
                f"edit_file failed: '{path}' changed since it was read "
                f"(expected sha256 {expected_hash[:12]}…, found {actual[:12]}…). Re-read the file."
            )
        new_text = apply_text_edits(data.decode("utf-8"), edits)
        atomic_write_bytes(path, new_text.encode("utf-8"))
        return {"path": path, "edited": True, "edits_applied": len(edits)}

    if tool_name == "create_directory":
        # Epic 2, E2-04: parents=True — one call creates the whole chain.
        path = args.get("path")
        if not path:
            raise ValueError("Directory path is required")
        Path(path).mkdir(parents=True, exist_ok=True)
        return {"path": path, "created": True}

    if tool_name == "run_checks":
        # Epic 2, E2-11: run the repo's own detected checks and return
        # the same parsed summary the engine's implementation produces
        # (shared logic in aether_common.checks).
        from aether_common.checks import (
            CHECK_TIMEOUT_S,
            detect_checks,
            format_summary,
            parse_result,
        )

        path = args.get("path")
        if not path:
            raise ValueError("Repository path is required")
        root = Path(path)
        if not root.is_dir():
            raise ValueError(f"run_checks failed: not a directory: {path}")
        # E2-F14: the caller may override the detected commands
        # (argv per check kind); detect_checks validates the entries.
        commands = args.get("commands")
        results = []
        for check in detect_checks(root, commands if isinstance(commands, dict) else None):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *check["argv"], cwd=str(root),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CHECK_TIMEOUT_S)
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    results.append({"name": check["name"], "ok": False, "passed": 0, "failed": 0,
                                    "errors": 1, "failures": [f"timed out after {CHECK_TIMEOUT_S}s"]})
                    continue
                output = stdout.decode(errors="ignore") + stderr.decode(errors="ignore")
                results.append({**parse_result(check["kind"], output, proc.returncode or 0), "name": check["name"]})
            except OSError as exc:
                results.append({"name": check["name"], "ok": False, "passed": 0, "failed": 0,
                                "errors": 1, "failures": [f"could not run: {exc}"]})
        return {"path": path, "summary": format_summary(str(root), results)}

    raise ValueError(f"Unknown tool: {tool_name!r}")


def _audit_event(action: str, details: dict) -> None:
    """Append one audit record in AuditLogger.log_event's exact shape."""
    try:
        DEFAULT_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEFAULT_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "ENGINE_EVENT", "ts": time.time(),
                                "action": action, "details": details}) + "\n")
    except Exception:  # noqa: BLE001, S110 — deliberate: audit writes are best-effort in the worker; a logging failure must never change an execution outcome.
        pass


def _refuse(action: str, tool_name, reason: str) -> int:
    """Record a refusal in the audit log (best-effort) and exit non-zero.

    The refusal itself never depends on audit success: a broken audit
    log must not turn a refused execution into an allowed one.
    """
    _audit_event(action, {"tool": tool_name, "reason": reason})
    print(json.dumps({"status": "refused", "reason": reason}), file=sys.stderr)
    return 1


async def main() -> int:
    # Payload contract (S2-M4): exactly one JSON document on stdin.
    # No argv form exists — see the module docstring. The read is
    # bounded in bytes and in time (E9-W3): the engine writes the
    # payload and closes stdin immediately (communicate(input=...)),
    # so a conforming read finishes at once, but a writer that holds
    # the pipe open — or an endless stdin — is refused after
    # PAYLOAD_READ_TIMEOUT_S / MAX_PAYLOAD_BYTES instead of stalling
    # or swelling this worker without limit. The blocking read runs
    # on a DAEMON thread, polled against the deadline below: a
    # refused worker must still be able to exit while its read is
    # stuck (an executor thread would be joined at loop shutdown
    # and hold the process hostage). Nothing else runs until the
    # payload is in hand, which is the ordering the auth check
    # below relies on.
    read_q: queue.SimpleQueue = queue.SimpleQueue()

    def _read_stdin() -> None:
        try:
            read_q.put(sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1))
        except Exception as e:  # noqa: BLE001 — reported as a refusal below
            read_q.put(e)

    threading.Thread(target=_read_stdin, name="aether-worker-stdin",
                     daemon=True).start()
    deadline = time.monotonic() + PAYLOAD_READ_TIMEOUT_S
    raw_payload = None
    while raw_payload is None:
        try:
            item = read_q.get_nowait()
        except queue.Empty:
            if time.monotonic() >= deadline:
                return _refuse("WORKER_AUTH_REFUSED", None,
                               "payload_read_timeout")
            await asyncio.sleep(0.01)
            continue
        if isinstance(item, Exception):
            return _refuse("WORKER_AUTH_REFUSED", None,
                           f"payload_read_error: {item}")
        raw_payload = item
    if len(raw_payload) > MAX_PAYLOAD_BYTES:
        return _refuse("WORKER_AUTH_REFUSED", None, "payload_too_large")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as e:
        return _refuse("WORKER_EXECUTION_REFUSED", None,
                       f"Invalid payload JSON: {e}")
    if not isinstance(payload, dict):
        return _refuse("WORKER_EXECUTION_REFUSED", None,
                       "Invalid payload: expected a JSON object")

    # Tool name is audit metadata only — never log args (they may hold
    # file contents, shell commands, or secrets).
    tool_name = payload.get("tool_name")

    # ── Authenticate BEFORE any dispatch (ADR 0003 token, Option 2) ──
    provided = os.environ.get(WORKER_TOKEN_ENV, "")
    try:
        expected = read_token()
    except (FileNotFoundError, ValueError) as e:
        return _refuse("WORKER_AUTH_REFUSED", tool_name,
                       f"token_file_unavailable: {e}")
    if not verify_token(provided, expected):
        return _refuse("WORKER_AUTH_REFUSED", tool_name,
                       "missing_token" if not provided else "invalid_token")

    try:
        result = await run_worker(payload)
    except ValueError as e:
        return _refuse("WORKER_EXECUTION_REFUSED", tool_name, str(e))
    except Exception as e:  # noqa: BLE001 — E9-W1: the docstring contract is that EVERY malformed payload is refused and audited; a non-ValueError shape (args not a dict, an unhashable tool_name, an OS error from the tool) must not escape as a bare traceback with no audit record.
        return _refuse("WORKER_EXECUTION_REFUSED", tool_name,
                       f"{type(e).__name__}: {e}")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
