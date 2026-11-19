"""mini-SWE-agent coder backend — the P4a cheap tier.

This is the ONLY module that imports ``minisweagent`` (and it does
so only inside the child-process entry point): the library's API
churn stops here, per the plan.

## Containment (the step-0 spike's answer, with evidence)

mini-SWE-agent 1.17.5 has **no read-only or propose-only mode**:
its ``LocalEnvironment`` is a raw shell (``subprocess.run(...,
shell=True)`` in a chosen cwd). Propose-mode is therefore something
the wrapper builds around it, not a mode it offers:

- the child runs in a **throwaway git worktree** of the delegated
  repo (checked out at HEAD) that the wrapper creates before spawn
  and removes after the diff is captured — the live tree is never
  the child's cwd and nothing the child produces is applied to it;
- the child's environment is the engine's environment **minus
  secrets** (see :func:`scrub_environment`), plus exactly the one
  LiteLLM route the run needs, injected at spawn. The library's own
  global ``.env`` load is neutralised by pointing
  ``MSWEA_GLOBAL_CONFIG_DIR`` at an empty per-run directory, so a
  key sitting in a user's mini-SWE config cannot leak back in;
- after the run, the wrapper compares the live tree's
  ``git status --porcelain`` against its pre-run snapshot: any
  change is a containment breach and the run reports ``failed``,
  whatever the child claimed;
- the returned diff is captured from the worktree
  (``git add -A -f`` + ``git diff --cached``), so every write the
  child made *to the workspace* is visible in it.

What this does NOT claim: a bash agent can address absolute paths
outside its cwd (the spike demonstrated a ``../`` escape landing
outside the worktree, invisible to the diff). The worktree guards
the delegated tree — the tree the diff is proposed against — and
the status check proves that guard held; it is not a general
filesystem sandbox. A future hardening (container/VM execution)
belongs to P4b's Docker decision, not to this wrapper.

## Budget, timeout, cancel

The wrapper — never the backend — enforces the budget
(:class:`~.coder_backend.CoderBudget`):

- **USD + tokens:** the child checks both between model steps
  (mini-SWE's own cost limit for USD; a counting model wrapper for
  tokens) and stops with ``LimitsExceeded`` → ``budget-stopped``.
- **Wall clock:** the child checks its deadline between steps, and
  the wrapper independently kills the child's process group when
  the budget plus a grace period elapses (a child stuck inside one
  long shell command cannot check its own deadline).
- **Cancel:** :meth:`MiniSweCoderBackend.cancel` kills the child's
  process group; the run returns ``failed`` ("cancelled by caller").

The transcript is appended to a progress JSONL after **every**
message the agent records, flushed per line, so even a killed run
leaves a transcript that is complete through the last finished
step; the assembled ``transcript.json`` carries ``complete: false``
for such runs rather than pretending otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aether_engine.langgraph.backends.coder_backend import (
    STATUS_BUDGET_STOPPED,
    STATUS_DONE,
    STATUS_FAILED,
    CoderTaskRequest,
    CoderTaskResult,
)

logger = logging.getLogger("aether_engine.langgraph.backends.coder_miniswe")

BACKEND_NAME = "miniswe"

_CHILD_MODULE = "aether_engine.langgraph.backends.coder_miniswe"

#: Env var names that mark a value as a secret for scrub purposes.
_SECRETISH = re.compile(
    r"(API_KEY|_TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY)", re.IGNORECASE)

#: LiteLLM model-prefix → the env var LiteLLM reads the key from.
#: ``None`` = the route needs no key (local runtimes) or uses a
#: credential file/flow a bare key cannot express (vertex, bedrock)
#: — those routes run keyless here and fail at the provider if they
#: truly need one, which the result reports honestly.
_PREFIX_ENV_VARS: dict[str, str | None] = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "azure": "AZURE_API_KEY",
    "ollama": None,
    "vertex_ai": None,
    "bedrock": None,
}

#: Per-shell-command timeout inside the child (mini-SWE's
#: LocalEnvironment default is 30s — too tight for test suites).
_PER_COMMAND_TIMEOUT_S = 120

#: Wrapper-side grace past the wall-clock budget before it kills
#: the child group (the child normally stops itself first).
_KILL_GRACE_S = 30.0

_SUMMARY_CAP = 4000


def scrub_environment(base: dict[str, str]) -> dict[str, str]:
    """``base`` minus every secret-shaped variable. Route variables
    are injected AFTER this scrub (see :meth:`MiniSweCoderBackend.run`),
    so an injected ``*_API_KEY`` survives while every inherited one
    is dropped."""
    return {k: v for k, v in base.items() if not _SECRETISH.search(k)}


def route_env_for_model(model_alias: str, api_key: str | None) -> dict[str, str]:
    """The one LiteLLM route's env: the key variable for the model's
    prefix. Empty when there is no usable key (keyless local routes,
    or the provider's redacted placeholder) or the prefix's route
    takes no bare key."""
    if not api_key or api_key == "<redacted>":
        return {}
    prefix = model_alias.split("/", 1)[0] if "/" in model_alias else "openai"
    var = _PREFIX_ENV_VARS.get(prefix, "OPENAI_API_KEY")
    if var is None:
        return {}
    return {var: api_key}


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


class MiniSweCoderBackend:
    """The mini-SWE wrapper. One instance may run one task at a
    time; ``cancel()`` targets the in-flight run."""

    name = BACKEND_NAME

    def __init__(
        self,
        *,
        route_env: dict[str, str] | None = None,
        model_kwargs: dict | None = None,
        artifacts_root: str | Path | None = None,
        child_argv: list[str] | None = None,
        kill_grace_s: float = _KILL_GRACE_S,
    ) -> None:
        self._route_env = dict(route_env or {})
        self._model_kwargs = dict(model_kwargs or {})
        self._artifacts_root = (
            Path(artifacts_root) if artifacts_root is not None
            else Path.home() / ".aether" / "coder_backend_runs")
        # Test seam: an alternate child command (the wrapper appends
        # [request_path, result_path] to it). Production uses the
        # module's own child entry point below.
        self._child_argv = list(child_argv) if child_argv else None
        self._kill_grace_s = kill_grace_s
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    # -- lifecycle -------------------------------------------------

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

    # -- the run ---------------------------------------------------

    def run(self, request: CoderTaskRequest) -> CoderTaskResult:
        self._cancelled = False
        run_id = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
        run_dir = self._artifacts_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        repo = Path(request.repo_path)

        def _early_failure(reason: str) -> CoderTaskResult:
            transcript = self._assemble_transcript(
                run_dir, request, complete=True, exit_status="WrapperError",
                note=reason)
            return CoderTaskResult(
                status=STATUS_FAILED, diff="", summary=reason,
                files_touched=[], cost_actual=0.0,
                transcript_ref=str(transcript), backend=BACKEND_NAME)

        try:
            live_before = _git(repo, "status", "--porcelain")
            _git(repo, "rev-parse", "--verify", "HEAD")
        except Exception as exc:  # noqa: BLE001 — reported as the run's result, per the contract.
            return _early_failure(f"delegated repo is not usable: {exc}")

        wt_root = Path(tempfile.mkdtemp(prefix="aether-miniswe-wt-"))
        worktree = wt_root / "wt"
        try:
            _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(wt_root, ignore_errors=True)
            return _early_failure(f"could not create the run worktree: {exc}")

        budget = request.budget
        request_path = run_dir / "child_request.json"
        result_path = run_dir / "child_result.json"
        progress_path = run_dir / "transcript.progress.jsonl"
        mswea_cfg = run_dir / "mswea-config"
        mswea_cfg.mkdir(exist_ok=True)
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
            "per_command_timeout_s": _PER_COMMAND_TIMEOUT_S,
        }), encoding="utf-8")

        env = scrub_environment(dict(os.environ))
        env.update(self._route_env)
        env["MSWEA_GLOBAL_CONFIG_DIR"] = str(mswea_cfg)
        env["MSWEA_SILENT_STARTUP"] = "1"

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

    # -- transcript --------------------------------------------------

    def _assemble_transcript(
        self, run_dir: Path, request: CoderTaskRequest, *,
        complete: bool, exit_status: str, note: str | None,
    ) -> Path:
        messages: list = []
        progress = run_dir / "transcript.progress.jsonl"
        if progress.exists():
            for line in progress.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    messages.append({"role": "unparsed", "content": line})
        path = run_dir / "transcript.json"
        path.write_text(json.dumps({
            "backend": BACKEND_NAME,
            "model": request.model_alias,
            "repo_path": request.repo_path,
            "task": _task_text(request),
            "exit_status": exit_status,
            "complete": complete,
            "note": note,
            "messages": messages,
        }, indent=1, default=str), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Child-process entry point — the only place minisweagent is imported.
# Invoked as: python -m aether_engine.langgraph.backends.coder_miniswe
#             <request.json> <result.json>
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


def _run_child_agent(req: dict, result: dict) -> None:
    import yaml
    from minisweagent import package_dir
    from minisweagent.agents.default import DefaultAgent, LimitsExceeded
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.models.litellm_model import LitellmModel

    budget = req.get("budget") or {}
    token_budget = int(budget.get("tokens") or 0)
    wall = budget.get("wall_clock_seconds")
    deadline = None if not wall else time.monotonic() + float(wall)
    progress_fh = open(req["progress_path"], "a", buffering=1,  # noqa: SIM115 — lives for the child's whole run.
                       encoding="utf-8")

    class _CountingModel(LitellmModel):
        """LitellmModel + a running token total (the base class
        tracks cost and calls, not tokens)."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.tokens_total = 0

        def _query(self, messages, **kwargs):
            response = super()._query(messages, **kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                total = getattr(usage, "total_tokens", None)
                if total is None:
                    total = ((getattr(usage, "prompt_tokens", 0) or 0)
                             + (getattr(usage, "completion_tokens", 0) or 0))
                self.tokens_total += int(total or 0)
            return response

    class _BudgetAgent(DefaultAgent):
        """DefaultAgent + token/wall checks between steps (its own
        query() already checks the USD cost limit) + the progress
        transcript: every recorded message is appended to the
        progress JSONL and flushed, so a killed run's transcript is
        complete through the last finished step."""

        def query(self):
            if token_budget and self.model.tokens_total > token_budget:
                raise LimitsExceeded(
                    f"token budget exhausted ({self.model.tokens_total} tokens)")
            if deadline is not None and time.monotonic() > deadline:
                raise LimitsExceeded("wall-clock budget exhausted")
            return super().query()

        def add_message(self, role, content, **kwargs):
            super().add_message(role, content, **kwargs)
            progress_fh.write(
                json.dumps(self.messages[-1], default=str) + "\n")

    cfg = yaml.safe_load(
        (package_dir / "config" / "default.yaml").read_text(encoding="utf-8"))
    agent_kwargs = {
        k: v for k, v in (cfg.get("agent") or {}).items()
        if k not in ("step_limit", "cost_limit")}
    env_vars = dict(((cfg.get("environment") or {}).get("env")) or {})
    model_kwargs = dict(((cfg.get("model") or {}).get("model_kwargs")) or {})
    model_kwargs.update(req.get("model_kwargs") or {})

    model = _CountingModel(
        model_name=req["model_name"], model_kwargs=model_kwargs,
        cost_tracking="ignore_errors")
    env = LocalEnvironment(
        cwd=req["worktree"],
        timeout=int(req.get("per_command_timeout_s") or _PER_COMMAND_TIMEOUT_S),
        env=env_vars)
    agent = _BudgetAgent(
        model, env, step_limit=0,
        cost_limit=float(budget.get("usd") or 0.0), **agent_kwargs)
    exit_status, message = agent.run(req["task"])
    result.update({
        "exit_status": exit_status,
        "message": message,
        "cost": model.cost,
        "tokens": model.tokens_total,
        "n_calls": model.n_calls,
    })


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv))
