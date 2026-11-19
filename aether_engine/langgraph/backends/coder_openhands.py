"""OpenHands coder backend — the P4b strong tier.

This is the ONLY module that imports ``openhands`` (and it does so
only inside the child-process entry point): the V1 SDK is young and
moving (plan risk #1), so its API churn stops here, exactly as
mini-SWE-agent's does in :mod:`coder_miniswe`. The wrapper half of
this module mirrors that module's discipline deliberately — same
worktree containment, same budget/timeout/cancel enforcement, same
transcript contract — so P2a's cascade cannot tell the backends
apart except through quality and cost.

## Availability (honest-unavailable is a first-class path)

The strong tier needs two things the cheap tier does not:

- the OpenHands SDK packages (``openhands-sdk``, ``openhands-tools``,
  ``openhands-workspace``, all MIT) importable by the child process;
- a working Docker installation — the SDK's ``DockerWorkspace``
  sandbox is the whole point of this tier.

Neither is guaranteed. In particular the SDK is **not** a dependency
of the engine today: the current SDK release line (1.62.x) requires
``litellm<1.106`` while the engine pins the verified
``litellm==1.108.5`` / ``mcp==1.30.0`` pair (pinning programme,
15 Nov 2026), so the packages cannot simply join ``pyproject.toml``
— which SDK release to pin, and whether it lives in the engine
environment or its own, is an install decision that lands with the
owner's Docker decision. Until then :func:`probe_availability`
reports the backend unavailable-with-reason, :meth:`run
<OpenHandsCoderBackend.run>` returns that reason as a ``failed``
result (never raises, never fakes a run), and callers that can fall
back — P2a's cascade — treat "strong tier unavailable" as "stay
native", per the plan.

## Containment

Stronger than the cheap tier's, by construction: the agent's tools
execute inside the Docker sandbox, not in a host shell. The wrapper
still prepares a **throwaway git worktree** of the delegated repo
and the sandbox works against that worktree — never the live tree —
and the wrapper still:

- captures the returned diff from the worktree, so every write the
  run made *to the workspace* is visible in it;
- compares the live tree's ``git status --porcelain`` against its
  pre-run snapshot and reports any change as a containment breach;
- sweeps containers after the run: it snapshots the agent-server
  container ids before spawn and force-removes any new one after
  the child exits, so a hard-killed child cannot orphan its
  sandbox (teardown is enforced, not assumed — plan step 1). The
  sweep is keyed on the sandbox image and the run window; a foreign
  container from a *concurrent* run of the same image started inside
  the window could be swept too, which is why one backend instance
  runs one task at a time.

Writes to the real tree happen only through Aether's existing
approval flow, on the returned diff — never from the container.

## Budget, timeout, cancel

The wrapper — never the backend — enforces the budget
(:class:`~.coder_backend.CoderBudget`), same as P4a:

- **USD + tokens:** the child checks both after every SDK event
  (the conversation's accumulated stats) and pauses the
  conversation when either is exceeded → ``budget-stopped``.
- **Wall clock:** the child checks its deadline between events,
  and the wrapper independently kills the child's process group
  when the budget plus a grace period elapses.
- **Cancel:** :meth:`OpenHandsCoderBackend.cancel` kills the
  child's process group; the run returns ``failed`` ("cancelled by
  caller") and the container sweep still runs.

The transcript is appended to a progress JSONL after **every**
event the SDK reports, flushed per line, so even a killed run
leaves a transcript complete through the last finished event; the
assembled ``transcript.json`` carries ``complete: false`` for such
runs rather than pretending otherwise.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aether_engine.langgraph.backends.coder_backend import (
    STATUS_BUDGET_STOPPED,
    STATUS_DONE,
    STATUS_FAILED,
    CoderTaskRequest,
    CoderTaskResult,
)
from aether_engine.langgraph.backends.coder_miniswe import scrub_environment

logger = logging.getLogger("aether_engine.langgraph.backends.coder_openhands")

BACKEND_NAME = "openhands"

_CHILD_MODULE = "aether_engine.langgraph.backends.coder_openhands"

#: The SDK's agent-server image the Docker sandbox runs. The tag
#: must track the pinned SDK release when the packages land (see
#: the module docstring's availability note); ``latest-python`` is
#: the SDK's documented default line until that pin exists.
DEFAULT_SERVER_IMAGE = "ghcr.io/openhands/agent-server:latest-python"

#: Wrapper-side grace past the wall-clock budget before it kills
#: the child group (the child normally stops itself first).
_KILL_GRACE_S = 30.0

#: How long the Docker daemon probe may take before the daemon is
#: judged unreachable.
_DAEMON_PROBE_TIMEOUT_S = 10.0

_SUMMARY_CAP = 4000

#: Env vars the wrapper uses to hand the child its one LiteLLM
#: route. The key travels ONLY in the child's environment — never
#: in the request file (same rule as P4a).
_ROUTE_KEY_ENV = "AETHER_CODER_ROUTE_API_KEY"
_ROUTE_BASE_ENV = "AETHER_CODER_ROUTE_BASE_URL"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenHandsAvailability:
    """The strong tier's availability verdict. ``reason`` is empty
    when available; otherwise it is the user-facing explanation of
    exactly what is missing — the honest-unavailable surface the
    plan requires, suitable for a delegation-log entry, a Settings
    row, or a result summary verbatim."""

    available: bool
    reason: str = ""


def probe_sdk_installed() -> bool:
    """True when the OpenHands SDK packages are importable here.
    Probed with ``find_spec`` only — importing the SDK is the
    child's job, never the wrapper's."""
    try:
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("openhands.sdk", "openhands.tools",
                         "openhands.workspace"))
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def probe_docker_daemon(docker_cli: str) -> bool:
    """True when ``docker info`` answers — the CLI exists AND the
    daemon is reachable. A wedged daemon must read as unavailable,
    not hang the probe."""
    try:
        proc = subprocess.run(
            [docker_cli, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True,
            timeout=_DAEMON_PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def probe_availability(
    *,
    sdk_installed: bool | None = None,
    docker_cli: str | None = None,
    daemon_reachable: bool | None = None,
) -> OpenHandsAvailability:
    """Judge the strong tier's availability, cheapest check first.
    Each probe may be injected (tests); ``None`` runs the real one.
    The reason names the FIRST missing piece, in the order a user
    would fix them: install the SDK, install Docker, start Docker.
    """
    if sdk_installed is None:
        sdk_installed = probe_sdk_installed()
    if not sdk_installed:
        return OpenHandsAvailability(
            available=False,
            reason=(
                "the OpenHands SDK packages (openhands-sdk, "
                "openhands-tools, openhands-workspace) are not "
                "installed in the engine's environment"))
    if docker_cli is None:
        docker_cli = shutil.which("docker") or ""
    if not docker_cli:
        return OpenHandsAvailability(
            available=False,
            reason=(
                "Docker is not installed (no 'docker' executable "
                "found) — the strong tier's sandbox is a Docker "
                "workspace"))
    if daemon_reachable is None:
        daemon_reachable = probe_docker_daemon(docker_cli)
    if not daemon_reachable:
        return OpenHandsAvailability(
            available=False,
            reason=(
                "the Docker daemon is not reachable ('docker info' "
                "failed) — start Docker and try again"))
    return OpenHandsAvailability(available=True)


# ---------------------------------------------------------------------------
# Shared helpers (mirrored from coder_miniswe on purpose: each backend
# module stays self-contained so one library's churn cannot reach the
# other — see both modules' docstrings).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args[:2])} failed in {repo}: "
            f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def _task_text(request: CoderTaskRequest) -> str:
    text = request.task_briefing
    ctx = request.attempt_context
    if ctx is not None and (ctx.prior_diff or ctx.rejection_reason):
        text += (
            "\n\n## Prior attempt\n"
            "A previous attempt at this task produced the diff below "
            "and was rejected. It is NOT applied to the current tree "
            "— start from the tree as it stands and address the "
            "rejection.\n"
        )
        if ctx.rejection_reason:
            text += f"\nRejection reason: {ctx.rejection_reason}\n"
        if ctx.prior_diff:
            text += f"\n```diff\n{ctx.prior_diff}\n```\n"
    return text


class OpenHandsCoderBackend:
    """The OpenHands wrapper. One instance may run one task at a
    time; ``cancel()`` targets the in-flight run."""

    name = BACKEND_NAME

    def __init__(
        self,
        *,
        route_env: dict[str, str] | None = None,
        model_kwargs: dict | None = None,
        artifacts_root: str | Path | None = None,
        server_image: str = DEFAULT_SERVER_IMAGE,
        child_argv: list[str] | None = None,
        docker_cli: str | None = None,
        availability: OpenHandsAvailability | None = None,
        kill_grace_s: float = _KILL_GRACE_S,
    ) -> None:
        self._route_env = dict(route_env or {})
        self._model_kwargs = dict(model_kwargs or {})
        self._artifacts_root = (
            Path(artifacts_root) if artifacts_root is not None
            else Path.home() / ".aether" / "coder_backend_runs")
        self._server_image = server_image
        # Test seams: an alternate child command (the wrapper appends
        # [request_path, result_path] to it) and an injected
        # availability verdict. Production uses the module's own
        # child entry point and the real probes.
        self._child_argv = list(child_argv) if child_argv else None
        self._docker_cli = docker_cli
        self._availability_override = availability
        self._kill_grace_s = kill_grace_s
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    # -- availability ------------------------------------------------

    def availability(self) -> OpenHandsAvailability:
        """The current availability verdict (probed live unless an
        override was injected). Callers that can fall back — the
        P2a cascade — consult this BEFORE delegating; ``run()``
        re-checks it regardless."""
        if self._availability_override is not None:
            return self._availability_override
        return probe_availability(docker_cli=self._docker_cli)

    # -- lifecycle ---------------------------------------------------

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc is not None and self._proc.poll() is None:
            self._kill_group(self._proc)

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    # -- container sweep ----------------------------------------------

    def _docker(self, *args: str) -> str:
        """One docker CLI call. Only invoked when availability was
        already judged good (or a CLI path was injected for tests);
        failures degrade to empty output — the sweep is a guarantee
        attempt layered on the workspace's own teardown, and a
        failing sweep is logged, never fatal to the result."""
        cli = self._docker_cli or shutil.which("docker") or ""
        if not cli:
            return ""
        try:
            proc = subprocess.run(
                [cli, *args], capture_output=True, text=True,
                timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("docker %s failed: %s", args[0], exc)
            return ""
        if proc.returncode != 0:
            logger.debug("docker %s exited %d: %s",
                         args[0], proc.returncode,
                         proc.stderr.strip()[:200])
            return ""
        return proc.stdout

    def _sandbox_container_ids(self) -> set[str]:
        out = self._docker(
            "ps", "-aq", "--filter", f"ancestor={self._server_image}")
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _sweep_new_containers(self, before: set[str]) -> None:
        """Force-remove every sandbox container that appeared during
        the run and is not the workspace's own already-stopped one —
        in practice: any id not in ``before``. This is what makes
        "no orphaned containers" true even when the child was
        SIGKILLed mid-run and its workspace teardown never ran."""
        for cid in self._sandbox_container_ids() - before:
            self._docker("rm", "-f", cid)

    # -- the run -----------------------------------------------------

    def run(self, request: CoderTaskRequest) -> CoderTaskResult:
        self._cancelled = False
        run_id = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
        run_dir = self._artifacts_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        repo = Path(request.repo_path)

        def _early_failure(reason: str, exit_status: str) -> CoderTaskResult:
            transcript = self._assemble_transcript(
                run_dir, request, complete=True, exit_status=exit_status,
                note=reason)
            return CoderTaskResult(
                status=STATUS_FAILED, diff="", summary=reason,
                files_touched=[], cost_actual=0.0,
                transcript_ref=str(transcript), backend=BACKEND_NAME)

        # Honest unavailability, first: no worktree, no child, no
        # container — a failed result that says exactly what is
        # missing. The cascade reads this as "stay native".
        verdict = self.availability()
        if not verdict.available:
            return _early_failure(
                f"Coder backend 'openhands' is unavailable: "
                f"{verdict.reason}.", "Unavailable")

        try:
            live_before = _git(repo, "status", "--porcelain")
            _git(repo, "rev-parse", "--verify", "HEAD")
        except Exception as exc:  # noqa: BLE001 — reported as the run's result, per the contract.
            return _early_failure(f"delegated repo is not usable: {exc}",
                                  "WrapperError")

        wt_root = Path(tempfile.mkdtemp(prefix="aether-openhands-wt-"))
        worktree = wt_root / "wt"
        try:
            _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(wt_root, ignore_errors=True)
            return _early_failure(f"could not create the run worktree: {exc}",
                                  "WrapperError")

        budget = request.budget
        request_path = run_dir / "child_request.json"
        result_path = run_dir / "child_result.json"
        progress_path = run_dir / "transcript.progress.jsonl"
        request_path.write_text(json.dumps({
            "task": _task_text(request),
            "model_name": request.model_alias,
            "model_kwargs": self._model_kwargs,
            "budget": {
                "tokens": budget.tokens,
                "usd": budget.usd,
                "wall_clock_seconds": budget.wall_clock_seconds,
            },
            "worktree": str(worktree),
            "progress_path": str(progress_path),
            "server_image": self._server_image,
        }), encoding="utf-8")

        env = scrub_environment(dict(os.environ))
        env.update(self._route_env)
        # The child's one route, under dedicated names: the SDK's
        # LLM is constructed from these explicitly (and LiteLLM
        # inside the SDK sees the provider var from route_env too).
        route_key = next(iter(self._route_env.values()), None)
        if route_key:
            env[_ROUTE_KEY_ENV] = route_key
        base_url = self._model_kwargs.get("api_base")
        if base_url:
            env[_ROUTE_BASE_ENV] = str(base_url)

        containers_before = self._sandbox_container_ids()
        argv = self._child_argv or [sys.executable, "-m", _CHILD_MODULE]
        log_path = run_dir / "child.log"
        stop_reason: str | None = None  # "wall-clock" | "cancelled"
        with open(log_path, "w", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                [*argv, str(request_path), str(result_path)],
                cwd=str(worktree), env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                start_new_session=True)
            self._proc = proc
            wall = budget.wall_clock_seconds
            deadline = (None if wall is None
                        else time.monotonic() + wall + self._kill_grace_s)
            try:
                while True:
                    if self._cancelled:
                        stop_reason = "cancelled"
                        self._kill_group(proc)
                        break
                    try:
                        proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if deadline is not None and time.monotonic() > deadline:
                            stop_reason = "wall-clock"
                            self._kill_group(proc)
                            break
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._kill_group(proc)
                    proc.wait(timeout=10)
                self._proc = None
            # cancel() kills the group itself, so the wait loop above
            # usually observes the exit before it can observe the
            # flag — reconcile the reason here.
            if stop_reason is None and self._cancelled:
                stop_reason = "cancelled"
        # No orphaned containers, whatever happened above.
        self._sweep_new_containers(containers_before)

        child: dict = {}
        if result_path.exists():
            try:
                child = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                child = {}
        exit_status = str(child.get("exit_status") or "")
        complete = bool(child) and stop_reason is None

        # Capture the proposed diff from the worktree BEFORE removing it.
        diff = ""
        files_touched: list[str] = []
        try:
            _git(worktree, "add", "-A", "-f")
            diff = _git(worktree, "diff", "--cached")
            names = _git(worktree, "diff", "--cached", "--name-only")
            files_touched = [n for n in names.splitlines() if n.strip()]
        except Exception as exc:  # noqa: BLE001 — the capture failure becomes the result.
            exit_status = exit_status or "WrapperError"
            child.setdefault("message", f"diff capture failed: {exc}")
        (run_dir / "proposed.diff").write_text(diff, encoding="utf-8")
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree))
            _git(repo, "worktree", "prune")
        except Exception as exc:  # noqa: BLE001 — a stale worktree registration is untidy, not fatal.
            logger.debug("worktree cleanup for %s failed: %s", worktree, exc)
        shutil.rmtree(wt_root, ignore_errors=True)

        # Containment proof: the live tree must be exactly as found.
        breach = False
        try:
            breach = _git(repo, "status", "--porcelain") != live_before
        except Exception:  # noqa: BLE001 — cannot verify = cannot claim containment.
            breach = True

        transcript = self._assemble_transcript(
            run_dir, request, complete=complete,
            exit_status=exit_status or "Killed",
            note=("run stopped by the wrapper: " + stop_reason
                  if stop_reason else None))

        message = str(child.get("message") or "")
        if stop_reason == "cancelled":
            status, summary = STATUS_FAILED, "Run cancelled by caller."
        elif stop_reason == "wall-clock" or exit_status == "LimitsExceeded":
            status = STATUS_BUDGET_STOPPED
            summary = message or "Budget exhausted (wall clock)."
        elif exit_status == "Submitted":
            status, summary = STATUS_DONE, message
        else:
            status = STATUS_FAILED
            summary = message or f"Backend run failed ({exit_status or 'no result'})."
        if breach:
            status = STATUS_FAILED
            summary = ("CONTAINMENT BREACH: the live tree changed during "
                       "the run; the proposed diff must not be trusted. "
                       + summary)
        if len(summary) > _SUMMARY_CAP:
            summary = summary[:_SUMMARY_CAP] + "\n…[truncated; full text in transcript]"
        return CoderTaskResult(
            status=status, diff=diff, summary=summary,
            files_touched=files_touched,
            cost_actual=float(child.get("cost") or 0.0),
            transcript_ref=str(transcript), backend=BACKEND_NAME,
            tokens_actual=int(child.get("tokens") or 0))

    # -- transcript ----------------------------------------------------

    def _assemble_transcript(
        self, run_dir: Path, request: CoderTaskRequest, *,
        complete: bool, exit_status: str, note: str | None,
    ) -> Path:
        events: list = []
        progress = run_dir / "transcript.progress.jsonl"
        if progress.exists():
            for line in progress.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"kind": "unparsed", "content": line})
        path = run_dir / "transcript.json"
        path.write_text(json.dumps({
            "backend": BACKEND_NAME,
            "model": request.model_alias,
            "repo_path": request.repo_path,
            "task": _task_text(request),
            "exit_status": exit_status,
            "complete": complete,
            "note": note,
            "events": events,
        }, indent=1, default=str), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Child-process entry point — the only place openhands is imported.
# Invoked as: python -m aether_engine.langgraph.backends.coder_openhands
#             <request.json> <result.json>
#
# Written against the OpenHands V1 SDK (openhands-sdk / -tools /
# -workspace 1.x; arXiv 2511.03690 and the SDK docs): LLM + the
# default agent preset + a DockerWorkspace sandbox + a Conversation
# whose event callbacks stream the transcript and enforce the
# token/USD budgets. Every SDK attribute read is defensive
# (getattr with fallbacks): the SDK is pre-1.0-paced churn, the
# wrapper consumes only this child's plain-JSON result either way,
# and a shape drift must degrade the run's report — never crash the
# wrapper. No live verification of this path exists yet: it awaits
# the owner's Docker decision (see the module docstring).
# ---------------------------------------------------------------------------


def _child_main(argv: list[str]) -> int:
    request_path, result_path = argv[1], argv[2]
    result: dict = {"exit_status": "Error", "message": "", "cost": 0.0,
                    "tokens": 0, "n_calls": 0}
    try:
        req = json.loads(Path(request_path).read_text(encoding="utf-8"))
        _run_child_agent(req, result)
    except Exception as exc:  # noqa: BLE001 — the wrapper reads failures from the result file.
        result["message"] = f"{type(exc).__name__}: {exc}"
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    return 0


def _event_record(event) -> dict:
    """One SDK event as a transcript JSONL record. Defensive by
    design (see the section comment above)."""
    record: dict = {"kind": type(event).__name__}
    source = getattr(event, "source", None)
    if source is not None:
        record["source"] = str(getattr(source, "value", source))
    text = ""
    message = getattr(event, "llm_message", None)
    content = getattr(message, "content", None) if message is not None else None
    if isinstance(content, list):
        parts = [str(getattr(part, "text", "") or "") for part in content]
        text = "\n".join(part for part in parts if part)
    if not text:
        action = getattr(event, "action", None)
        if action is not None:
            text = str(action)
    record["content"] = text[:2000]
    return record


def _stats_totals(conversation) -> tuple[float, int, int]:
    """(cost USD, total tokens, model calls) from the conversation's
    accumulated stats, across every per-model metrics entry."""
    stats = getattr(getattr(conversation, "state", None), "stats", None)
    metrics_map = getattr(stats, "usage_to_metrics", None) or {}
    cost, tokens, calls = 0.0, 0, 0
    values = metrics_map.values() if hasattr(metrics_map, "values") else []
    for metrics in values:
        cost += float(getattr(metrics, "accumulated_cost", 0.0) or 0.0)
        usage = getattr(metrics, "accumulated_token_usage", None)
        if usage is not None:
            tokens += int((getattr(usage, "prompt_tokens", 0) or 0)
                          + (getattr(usage, "completion_tokens", 0) or 0))
        calls += len(getattr(metrics, "token_usages", []) or [])
    return cost, tokens, calls


def _final_agent_text(conversation) -> str:
    """The last agent message in the event log — the run's summary,
    the same role mini-SWE's submission message plays."""
    events = getattr(getattr(conversation, "state", None), "events", None)
    if events is None:
        return ""
    for event in reversed(list(events)):
        if type(event).__name__ != "MessageEvent":
            continue
        source = getattr(event, "source", None)
        if str(getattr(source, "value", source)) != "agent":
            continue
        record = _event_record(event)
        if record["content"]:
            return record["content"]
    return ""


def _run_child_agent(req: dict, result: dict) -> None:
    from openhands.sdk import LLM, Conversation
    from openhands.tools.preset.default import get_default_agent
    from openhands.workspace import DockerWorkspace

    budget = req.get("budget") or {}
    token_budget = int(budget.get("tokens") or 0)
    usd_budget = float(budget.get("usd") or 0.0)
    wall = budget.get("wall_clock_seconds")
    deadline = None if not wall else time.monotonic() + float(wall)
    progress_fh = open(req["progress_path"], "a", buffering=1,  # noqa: SIM115 — lives for the child's whole run.
                       encoding="utf-8")

    holder: dict = {}
    stopped = {"budget": False}

    def _on_event(event) -> None:
        progress_fh.write(json.dumps(_event_record(event), default=str) + "\n")
        conversation = holder.get("conversation")
        if conversation is None or stopped["budget"]:
            return
        cost, tokens, _calls = _stats_totals(conversation)
        over = ((token_budget and tokens > token_budget)
                or (usd_budget and cost > usd_budget)
                or (deadline is not None and time.monotonic() > deadline))
        if over:
            stopped["budget"] = True
            try:
                conversation.pause()
            except Exception as exc:  # noqa: BLE001 — the wrapper's wall-clock kill is the backstop.
                logger.debug("conversation.pause() failed: %s", exc)

    llm = LLM(
        model=req["model_name"],
        api_key=os.environ.get(_ROUTE_KEY_ENV) or None,
        base_url=os.environ.get(_ROUTE_BASE_ENV) or None)
    agent = get_default_agent(llm=llm)
    with DockerWorkspace(
            server_image=req["server_image"],
            working_dir=req["worktree"]) as workspace:
        conversation = holder["conversation"] = Conversation(
            agent=agent, workspace=workspace, callbacks=[_on_event])
        conversation.send_message(req["task"])
        conversation.run()

    cost, tokens, calls = _stats_totals(conversation)
    status_obj = getattr(conversation.state, "execution_status", None)
    status = str(getattr(status_obj, "value", status_obj) or "").lower()
    message = _final_agent_text(conversation)
    if stopped["budget"]:
        exit_status = "LimitsExceeded"
        message = message or "budget exhausted (tokens/USD/wall clock)"
    elif status == "finished":
        exit_status = "Submitted"
    elif status in ("error", "stuck"):
        exit_status = "Error"
        message = message or f"OpenHands conversation ended: {status}"
    else:
        exit_status = "Error"
        message = message or f"OpenHands conversation ended: {status or 'unknown'}"
    result.update({
        "exit_status": exit_status,
        "message": message,
        "cost": cost,
        "tokens": tokens,
        "n_calls": calls,
    })


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv))
