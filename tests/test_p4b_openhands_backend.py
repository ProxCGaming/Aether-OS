"""P4b — OpenHands coder backend: honest unavailability, wrapper
lifecycle against a fake Docker CLI + fake child, the real child
entry point against a stub SDK package, and the coder-node flag.

No live Docker, no live model, no real SDK anywhere in this file:

- availability tests inject the probes (or point them at a fake
  ``docker`` executable);
- wrapper tests drive the subprocess lifecycle with a FAKE CHILD
  script honouring the wrapper's argv contract (the ``child_argv``
  seam) and a FAKE DOCKER CLI script whose container state lives in
  a temp dir, so the no-orphaned-containers sweep is asserted, not
  assumed;
- the real child entry point runs as a subprocess with a STUB
  ``openhands`` package on PYTHONPATH that records how the child
  drove the SDK (LLM route, DockerWorkspace arguments, callbacks);
- coder-node tests substitute a stub backend at the node's
  construction seam.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.backends import coder_openhands
from aether_engine.langgraph.backends.coder_backend import (
    AttemptContext,
    CoderBudget,
    CoderTaskRequest,
    CoderTaskResult,
)
from aether_engine.langgraph.backends.coder_openhands import (
    OpenHandsAvailability,
    OpenHandsCoderBackend,
    _task_text,
    probe_availability,
)

# ---------------------------------------------------------------------------
# Toy repo + fake docker + fake child fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def toy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toy"
    repo.mkdir()
    (repo / "hello.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "toy")
    return repo


FAKE_DOCKER = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path

state = Path(os.environ["FAKE_DOCKER_STATE"])
args = sys.argv[1:]

if args[0] == "info":
    sys.exit(1 if os.environ.get("FAKE_DOCKER_DAEMON_DOWN") else 0)
if args[0] == "ps":
    f = state / "containers"
    if f.exists():
        sys.stdout.write(f.read_text())
    sys.exit(0)
if args[0] == "rm":
    cid = args[-1]
    with open(state / "removed", "a") as fh:
        fh.write(cid + "\n")
    f = state / "containers"
    ids = [l for l in f.read_text().splitlines()
           if l.strip() and l.strip() != cid] if f.exists() else []
    f.write_text("".join(i + "\n" for i in ids))
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture()
def fake_docker(tmp_path: Path) -> Path:
    # Executed directly as the `docker` CLI, so: shebang + exec bit.
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER)
    script.chmod(0o755)
    return script


@pytest.fixture()
def docker_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "docker-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    return state


FAKE_CHILD = r'''
import json, os, subprocess, sys, time
from pathlib import Path

req = json.loads(Path(sys.argv[-2]).read_text())
mode = os.environ.get("FAKE_OH_CHILD_MODE", "fix")
cwd = Path.cwd()

def note(kind, text):
    with open(req["progress_path"], "a") as fh:
        fh.write(json.dumps(
            {"kind": kind, "source": "agent", "content": text}) + "\n")

def finish(result, rc=0):
    Path(sys.argv[-1]).write_text(json.dumps(result))
    sys.exit(rc)

if mode == "fix":
    (cwd / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    (cwd / "notes.txt").write_text("done\n")
    # The sandbox container appears (and, realistically, the
    # workspace's own teardown "forgets" to remove it — the
    # wrapper's sweep is what must catch it).
    state = Path(os.environ["FAKE_DOCKER_STATE"])
    with open(state / "containers", "a") as fh:
        fh.write("c0ffee123\n")
    probe = {
        "route_key_present":
            os.environ.get("AETHER_CODER_ROUTE_API_KEY") == "route-key",
        "parent_secret_present": "PARENT_SECRET_API_KEY" in os.environ,
        "server_image": req["server_image"],
    }
    (cwd / "env_probe.json").write_text(json.dumps(probe))
    note("ActionEvent", "editing hello.py")
    note("MessageEvent", "Fixed add()")
    finish({"exit_status": "Submitted", "message": "Fixed add()",
            "cost": 0.42, "tokens": 150, "n_calls": 2})
elif mode == "limits":
    finish({"exit_status": "LimitsExceeded", "message": "cost limit hit",
            "cost": 3.0, "tokens": 999, "n_calls": 5})
elif mode == "error":
    finish({"exit_status": "Error", "message": "sandbox exploded",
            "cost": 0.0, "tokens": 0, "n_calls": 0}, rc=3)
elif mode == "sleep":
    note("MessageEvent", "started")
    time.sleep(60)
    finish({"exit_status": "Submitted", "message": "slept",
            "cost": 0.0, "tokens": 0, "n_calls": 0})
elif mode == "escape":
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, cwd=cwd).stdout.strip()
    live = Path(common).resolve().parent
    (live / "breach.txt").write_text("escaped\n")
    finish({"exit_status": "Submitted", "message": "done (and escaped)",
            "cost": 0.1, "tokens": 10, "n_calls": 1})
'''


@pytest.fixture()
def fake_child(tmp_path: Path) -> Path:
    script = tmp_path / "fake_oh_child.py"
    script.write_text(FAKE_CHILD)
    return script


def _backend(fake_child: Path, fake_docker: Path, artifacts: Path,
             **kw) -> OpenHandsCoderBackend:
    kw.setdefault("availability", OpenHandsAvailability(available=True))
    kw.setdefault("docker_cli", str(fake_docker))
    return OpenHandsCoderBackend(
        artifacts_root=artifacts,
        child_argv=[sys.executable, str(fake_child)], **kw)


def _request(repo: Path, **kw) -> CoderTaskRequest:
    kw.setdefault("task_briefing", "Fix the sign bug in add()")
    kw.setdefault("model_alias", "gemini/gemini-2.5-flash")
    return CoderTaskRequest(repo_path=str(repo), **kw)


# ---------------------------------------------------------------------------
# Availability — the honest-unavailable surface
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_sdk_missing_is_the_first_reason(self):
        verdict = probe_availability(
            sdk_installed=False, docker_cli="/usr/bin/docker",
            daemon_reachable=True)
        assert verdict.available is False
        assert "openhands-sdk" in verdict.reason

    def test_docker_missing_names_docker(self):
        verdict = probe_availability(
            sdk_installed=True, docker_cli="", daemon_reachable=True)
        assert verdict.available is False
        assert "Docker is not installed" in verdict.reason

    def test_daemon_down_names_the_daemon(self):
        verdict = probe_availability(
            sdk_installed=True, docker_cli="/usr/bin/docker",
            daemon_reachable=False)
        assert verdict.available is False
        assert "daemon is not reachable" in verdict.reason

    def test_all_present_is_available_with_no_reason(self):
        verdict = probe_availability(
            sdk_installed=True, docker_cli="/usr/bin/docker",
            daemon_reachable=True)
        assert verdict == OpenHandsAvailability(available=True, reason="")

    def test_real_probe_in_this_environment_is_unavailable(self):
        # This VM has neither the SDK nor Docker; the probe must say
        # so (whichever piece it names first), never claim available.
        verdict = probe_availability()
        assert verdict.available is False
        assert verdict.reason

    def test_daemon_probe_against_fake_docker(
            self, fake_docker, docker_state, monkeypatch):
        assert coder_openhands.probe_docker_daemon(str(fake_docker)) is True
        monkeypatch.setenv("FAKE_DOCKER_DAEMON_DOWN", "1")
        assert coder_openhands.probe_docker_daemon(str(fake_docker)) is False

    def test_run_returns_unavailable_without_spawning_anything(
            self, toy_repo, fake_child, fake_docker, tmp_path, monkeypatch):
        monkeypatch.setattr(
            coder_openhands, "probe_sdk_installed", lambda: False)
        marker = tmp_path / "child-ran"
        spy = tmp_path / "spy_child.py"
        spy.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
        backend = OpenHandsCoderBackend(
            artifacts_root=tmp_path / "runs",
            child_argv=[sys.executable, str(spy)],
            docker_cli=str(fake_docker))
        result = backend.run(_request(toy_repo))

        assert result.status == "failed"
        assert result.backend == "openhands"
        assert "unavailable" in result.summary
        assert "openhands-sdk" in result.summary
        assert result.diff == "" and result.files_touched == []
        assert result.cost_actual == 0.0
        # Nothing was spawned, no worktree was created.
        assert not marker.exists()
        assert "wt" not in _git(toy_repo, "worktree", "list")
        # …but the transcript still exists and tells the truth.
        transcript = json.loads(Path(result.transcript_ref).read_text())
        assert transcript["exit_status"] == "Unavailable"
        assert transcript["backend"] == "openhands"

    def test_run_names_docker_when_only_docker_is_missing(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setattr(
            coder_openhands, "probe_sdk_installed", lambda: True)
        backend = OpenHandsCoderBackend(
            artifacts_root=tmp_path / "runs",
            child_argv=[sys.executable, str(fake_child)], docker_cli="")
        result = backend.run(_request(toy_repo))
        assert result.status == "failed"
        assert "Docker is not installed" in result.summary

    def test_run_names_the_daemon_when_it_is_down(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setattr(
            coder_openhands, "probe_sdk_installed", lambda: True)
        monkeypatch.setenv("FAKE_DOCKER_DAEMON_DOWN", "1")
        backend = OpenHandsCoderBackend(
            artifacts_root=tmp_path / "runs",
            child_argv=[sys.executable, str(fake_child)],
            docker_cli=str(fake_docker))
        result = backend.run(_request(toy_repo))
        assert result.status == "failed"
        assert "daemon is not reachable" in result.summary


# ---------------------------------------------------------------------------
# Wrapper lifecycle (fake child + fake docker)
# ---------------------------------------------------------------------------


class TestWrapperLifecycle:
    def test_done_run_returns_diff_record_and_clean_live_tree(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "fix")
        monkeypatch.setenv("PARENT_SECRET_API_KEY", "should-not-leak")
        backend = _backend(
            fake_child, fake_docker, tmp_path / "runs",
            route_env={"GEMINI_API_KEY": "route-key"})
        result = backend.run(_request(toy_repo))

        assert result.status == "done"
        assert result.backend == "openhands"
        assert result.summary == "Fixed add()"
        assert result.cost_actual == 0.42
        assert result.tokens_actual == 150
        # Every write the child made is visible in the diff.
        assert "return a + b" in result.diff
        assert "notes.txt" in result.diff
        assert sorted(result.files_touched) == [
            "env_probe.json", "hello.py", "notes.txt"]
        # The child's env: parent secret scrubbed, the route key
        # present under the wrapper's dedicated name, and the child
        # was told the sandbox image the wrapper configured.
        assert '"parent_secret_present": false' in result.diff
        assert '"route_key_present": true' in result.diff
        assert "agent-server" in result.diff
        # Live tree untouched, worktree removed.
        assert _git(toy_repo, "status", "--porcelain") == ""
        assert "toy-wt" not in _git(toy_repo, "worktree", "list")
        assert (toy_repo / "hello.py").read_text().endswith("return a - b\n")
        # Transcript: complete, carrying the streamed SDK events.
        transcript = json.loads(Path(result.transcript_ref).read_text())
        assert transcript["complete"] is True
        assert transcript["exit_status"] == "Submitted"
        assert [e["kind"] for e in transcript["events"]] == [
            "ActionEvent", "MessageEvent"]
        # The sandbox container the child left behind was swept.
        removed = (docker_state / "removed").read_text().split()
        assert removed == ["c0ffee123"]
        assert (docker_state / "containers").read_text() == ""

    def test_preexisting_containers_are_never_swept(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        (docker_state / "containers").write_text("preexisting1\n")
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "limits")
        backend = _backend(fake_child, fake_docker, tmp_path / "runs")
        result = backend.run(_request(toy_repo))
        assert result.status == "budget-stopped"
        assert result.cost_actual == 3.0
        removed = docker_state / "removed"
        assert not removed.exists() or removed.read_text() == ""
        assert (docker_state / "containers").read_text() == "preexisting1\n"

    def test_child_error_maps_to_failed(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "error")
        result = _backend(
            fake_child, fake_docker, tmp_path / "runs").run(_request(toy_repo))
        assert result.status == "failed"
        assert "sandbox exploded" in result.summary

    def test_wall_clock_budget_kills_a_hung_child(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "sleep")
        backend = _backend(
            fake_child, fake_docker, tmp_path / "runs", kill_grace_s=0.5)
        started = time.monotonic()
        result = backend.run(_request(
            toy_repo, budget=CoderBudget(wall_clock_seconds=1.0)))
        elapsed = time.monotonic() - started
        assert result.status == "budget-stopped"
        assert elapsed < 20  # the 60s child was killed, not awaited
        transcript = json.loads(Path(result.transcript_ref).read_text())
        assert transcript["complete"] is False
        # …but the progress transcript still holds what finished.
        assert [e["content"] for e in transcript["events"]] == ["started"]

    def test_cancel_kills_the_child(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "sleep")
        backend = _backend(fake_child, fake_docker, tmp_path / "runs")
        outcome: dict = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                "result", backend.run(_request(toy_repo))))
        thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            runs = list((tmp_path / "runs").glob("*/transcript.progress.jsonl"))
            if runs and runs[0].read_text().strip():
                break
            time.sleep(0.1)
        backend.cancel()
        thread.join(timeout=20)
        assert not thread.is_alive()
        result = outcome["result"]
        assert result.status == "failed"
        assert "cancelled" in result.summary.lower()

    def test_live_tree_breach_fails_the_run(
            self, toy_repo, fake_child, fake_docker, docker_state,
            tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_OH_CHILD_MODE", "escape")
        result = _backend(
            fake_child, fake_docker, tmp_path / "runs").run(_request(toy_repo))
        assert result.status == "failed"
        assert "CONTAINMENT BREACH" in result.summary
        assert (toy_repo / "breach.txt").exists()  # the evidence stays
        (toy_repo / "breach.txt").unlink()

    def test_unusable_repo_fails_cleanly_with_a_transcript(
            self, fake_child, fake_docker, docker_state, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = _backend(
            fake_child, fake_docker, tmp_path / "runs").run(_request(plain))
        assert result.status == "failed"
        assert "not usable" in result.summary
        assert Path(result.transcript_ref).exists()

    def test_task_text_carries_attempt_context(self):
        req = CoderTaskRequest(
            task_briefing="Fix it", repo_path="/r", model_alias="m",
            attempt_context=AttemptContext(
                prior_diff="-a\n+b", rejection_reason="tests fail"))
        text = _task_text(req)
        assert "Fix it" in text and "tests fail" in text
        assert "-a\n+b" in text and "NOT applied" in text


# ---------------------------------------------------------------------------
# The real child, against a stub SDK package (no live SDK/Docker/model)
# ---------------------------------------------------------------------------

STUB_SDK = {
    "openhands/__init__.py": "",
    "openhands/tools/__init__.py": "",
    "openhands/tools/preset/__init__.py": "",
    "openhands/tools/preset/default.py": r'''
def get_default_agent(llm=None):
    return {"agent_for_llm": llm}
''',
    "openhands/workspace.py": r'''
import json, os


class DockerWorkspace:
    def __init__(self, server_image=None, working_dir=None):
        self.server_image = server_image
        self.working_dir = working_dir
        record = {"workspace": {"server_image": server_image,
                                "working_dir": working_dir}}
        with open(os.environ["OH_STUB_RECORD"], "a") as fh:
            fh.write(json.dumps(record) + "\n")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
''',
    "openhands/sdk.py": r'''
import json, os


class _Text:
    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, text):
        self.content = [_Text(text)]


class MessageEvent:
    def __init__(self, source, text):
        self.source = source
        self.llm_message = _Message(text)


class ActionEvent:
    def __init__(self, text):
        self.source = "agent"
        self.action = text
        self.llm_message = None


class _Usage:
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _Metrics:
    def __init__(self, cost, prompt, completion, calls):
        self.accumulated_cost = cost
        self.accumulated_token_usage = _Usage(prompt, completion)
        self.token_usages = [object()] * calls


class _Stats:
    def __init__(self, metrics):
        self.usage_to_metrics = {"model": metrics}


class _State:
    def __init__(self):
        self.events = []
        self.stats = _Stats(_Metrics(0.0, 0, 0, 0))
        self.execution_status = "idle"


class LLM:
    def __init__(self, model=None, api_key=None, base_url=None):
        record = {"llm": {"model": model, "api_key": api_key,
                          "base_url": base_url}}
        with open(os.environ["OH_STUB_RECORD"], "a") as fh:
            fh.write(json.dumps(record) + "\n")


class Conversation:
    def __init__(self, agent=None, workspace=None, callbacks=None):
        self.agent = agent
        self.workspace = workspace
        self.callbacks = callbacks or []
        self.state = _State()
        self._paused = False

    def _emit(self, event):
        self.state.events.append(event)
        for cb in self.callbacks:
            cb(event)

    def send_message(self, text):
        self._task = text
        self._emit(MessageEvent("user", text))

    def pause(self):
        self._paused = True
        self.state.execution_status = "paused"

    def run(self):
        scenario = os.environ.get("OH_STUB_SCENARIO", "finish")
        if scenario == "budget":
            # Stats already over any sane token budget; the first
            # event's callback must pause the conversation.
            self.state.stats = _Stats(_Metrics(0.10, 9000, 1000, 1))
            self._emit(ActionEvent("working"))
            if self._paused:
                return
            raise AssertionError("budget callback never paused")
        if scenario == "error":
            self.state.execution_status = "error"
            return
        self._emit(ActionEvent("editing hello.py"))
        self.state.stats = _Stats(_Metrics(0.42, 100, 50, 2))
        self._emit(MessageEvent("agent", "Fixed add() sign bug"))
        self.state.execution_status = "finished"
''',
}


@pytest.fixture()
def stub_sdk(tmp_path: Path) -> Path:
    root = tmp_path / "stub-sdk"
    for rel, content in STUB_SDK.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


class TestChildAgainstStubSdk:
    def _run_child(self, stub_sdk, tmp_path, monkeypatch, scenario,
                   budget=None):
        record = tmp_path / "record.jsonl"
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        request_path = run_dir / "request.json"
        result_path = run_dir / "result.json"
        request_path.write_text(json.dumps({
            "task": "Fix the sign bug in add() in hello.py",
            "model_name": "gemini/gemini-2.5-flash",
            "model_kwargs": {},
            "budget": budget or {"tokens": None, "usd": None,
                                 "wall_clock_seconds": None},
            "worktree": str(tmp_path / "wt"),
            "progress_path": str(run_dir / "progress.jsonl"),
            "server_image": "ghcr.io/openhands/agent-server:latest-python",
        }))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(stub_sdk)
        env["OH_STUB_RECORD"] = str(record)
        env["OH_STUB_SCENARIO"] = scenario
        env["AETHER_CODER_ROUTE_API_KEY"] = "route-key-123"
        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m",
             "aether_engine.langgraph.backends.coder_openhands",
             str(request_path), str(result_path)],
            cwd=repo_root, env=env, capture_output=True, text=True,
            timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        return (json.loads(result_path.read_text()),
                [json.loads(l) for l in record.read_text().splitlines()],
                (run_dir / "progress.jsonl").read_text())

    def test_finished_run_maps_to_submitted_with_route_and_stats(
            self, stub_sdk, tmp_path, monkeypatch):
        result, records, progress = self._run_child(
            stub_sdk, tmp_path, monkeypatch, "finish")
        assert result["exit_status"] == "Submitted"
        assert result["message"] == "Fixed add() sign bug"
        assert result["cost"] == 0.42
        assert result["tokens"] == 150
        assert result["n_calls"] == 2
        # The SDK was driven per the plan: the delegation's LiteLLM
        # alias + the route key from the env (never the request
        # file), and the Docker sandbox over the wrapper's worktree.
        llm = next(r["llm"] for r in records if "llm" in r)
        assert llm == {"model": "gemini/gemini-2.5-flash",
                       "api_key": "route-key-123", "base_url": None}
        ws = next(r["workspace"] for r in records if "workspace" in r)
        assert ws["server_image"].endswith("agent-server:latest-python")
        assert ws["working_dir"] == str(tmp_path / "wt")
        # The event stream reached the progress transcript.
        kinds = [json.loads(l)["kind"]
                 for l in progress.splitlines() if l.strip()]
        assert kinds == ["MessageEvent", "ActionEvent", "MessageEvent"]

    def test_budget_breach_pauses_and_maps_to_limits(
            self, stub_sdk, tmp_path, monkeypatch):
        result, _records, _progress = self._run_child(
            stub_sdk, tmp_path, monkeypatch, "budget",
            budget={"tokens": 100, "usd": None, "wall_clock_seconds": None})
        assert result["exit_status"] == "LimitsExceeded"
        assert result["tokens"] == 10000

    def test_error_status_maps_to_error(
            self, stub_sdk, tmp_path, monkeypatch):
        result, _records, _progress = self._run_child(
            stub_sdk, tmp_path, monkeypatch, "error")
        assert result["exit_status"] == "Error"
        assert "error" in result["message"]


# ---------------------------------------------------------------------------
# Coder-node integration (flag on/off, stub backend)
# ---------------------------------------------------------------------------


class _StubBackend:
    name = "openhands"

    def __init__(self, result: CoderTaskResult):
        self._result = result
        self.requests: list[CoderTaskRequest] = []

    def run(self, request: CoderTaskRequest) -> CoderTaskResult:
        self.requests.append(request)
        return self._result


def _state(**over) -> dict:
    state = {
        "messages": [{"role": "user", "content": "Fix the sign bug"}],
        "task_id": "task-p4b",
        "plan_mode": False,
        "plan": "",
        "context_briefing": "",
        "original_prompt": "Fix the sign bug",
    }
    state.update(over)
    return state


def _config(user_config, **configurable) -> dict:
    base = {"provider": object(), "user_config": user_config,
            "workspace_roots": ["/tmp/some-repo"]}
    base.update(configurable)
    return {"configurable": base}


class TestCoderNodeFlag:
    @pytest.mark.asyncio
    async def test_openhands_flag_routes_to_the_backend(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend(CoderTaskResult(
            status="done", diff="--- a/hello.py\n+++ b/hello.py\n",
            summary="Fixed add()", files_touched=["hello.py"],
            cost_actual=0.42, transcript_ref="/runs/x/transcript.json",
            backend="openhands", tokens_actual=150))
        monkeypatch.setattr(
            coder, "_build_openhands_backend",
            lambda config, provider: (stub, "gemini/gemini-2.5-flash"))
        monkeypatch.setattr(
            coder, "save_artifact", lambda *a: "/artifacts/coder_output.txt")

        out = await coder.coder_node(
            _state(), _config(UserConfig(coder_backend="openhands")))

        assert len(stub.requests) == 1
        assert stub.requests[0].repo_path == "/tmp/some-repo"
        assert stub.requests[0].model_alias == "gemini/gemini-2.5-flash"
        assert out["current_phase"] == "coding"
        content = out["messages"][0]["content"]
        assert "Fixed add()" in content
        assert "Proposed diff" in content and "hello.py" in content
        entry = out["delegation_log"][0]
        assert entry["backend"] == "openhands"
        assert entry["status"] == "done"
        assert entry["cost_actual"] == 0.42
        assert "decision" not in entry  # existing log readers skip it

    @pytest.mark.asyncio
    async def test_unavailable_backend_surfaces_as_an_honest_failure(
            self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend(CoderTaskResult(
            status="failed", diff="",
            summary=("Coder backend 'openhands' is unavailable: "
                     "Docker is not installed (no 'docker' executable "
                     "found) — the strong tier's sandbox is a Docker "
                     "workspace."),
            files_touched=[], cost_actual=0.0,
            transcript_ref="/runs/x/transcript.json", backend="openhands"))
        monkeypatch.setattr(
            coder, "_build_openhands_backend",
            lambda config, provider: (stub, "gemini/gemini-2.5-flash"))
        monkeypatch.setattr(
            coder, "save_artifact", lambda *a: "/artifacts/coder_output.txt")

        out = await coder.coder_node(
            _state(), _config(UserConfig(coder_backend="openhands")))
        content = out["messages"][0]["content"]
        assert "[coder backend: openhands — failed]" in content
        assert "unavailable" in content and "Docker" in content
        assert out["delegation_log"][0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_plan_mode_never_uses_the_backend(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        async def sentinel(spec, state, config, *, save_artifact=None):
            return {"sentinel": True}

        monkeypatch.setattr(coder, "run_specialist", sentinel)
        monkeypatch.setattr(
            coder, "_build_openhands_backend",
            lambda config, provider: (_ for _ in ()).throw(
                AssertionError("backend built in plan mode")))
        out = await coder.coder_node(
            _state(plan_mode=True),
            _config(UserConfig(coder_backend="openhands")))
        assert out == {"sentinel": True}

    def test_config_flag_defaults_and_validation(self):
        assert UserConfig().coder_backend == "native"
        assert UserConfig.from_dict(
            {"coder_backend": "openhands"}).coder_backend == "openhands"
        assert UserConfig.from_dict(
            {"coder_backend": "bogus"}).coder_backend == "native"
        assert UserConfig(coder_backend="openhands").to_dict()[
            "coder_backend"] == "openhands"


def test_backend_satisfies_the_protocol():
    from aether_engine.langgraph.backends.coder_backend import CoderBackend
    assert isinstance(OpenHandsCoderBackend(), CoderBackend)


# ---------------------------------------------------------------------------
# Opt-in live smoke (plan step 3) — a real SDK + Docker + model
# against the toy repo. Skipped unless explicitly enabled AND the
# backend actually reports itself available:
#   AETHER_P4B_LIVE_SMOKE=1 AETHER_P4B_SMOKE_MODEL=gemini/gemini-2.5-flash
# with Docker running and the route's API key in the environment.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AETHER_P4B_LIVE_SMOKE") != "1",
    reason="opt-in live smoke: set AETHER_P4B_LIVE_SMOKE=1")
def test_live_smoke(toy_repo, tmp_path):
    from aether_engine.langgraph.backends.coder_miniswe import (
        route_env_for_model,
    )

    backend = OpenHandsCoderBackend(artifacts_root=tmp_path / "runs")
    verdict = backend.availability()
    if not verdict.available:
        pytest.skip(f"strong tier unavailable here: {verdict.reason}")
    model = os.environ.get(
        "AETHER_P4B_SMOKE_MODEL", "gemini/gemini-2.5-flash")
    prefix = model.split("/", 1)[0] if "/" in model else "openai"
    key_var = {"gemini": "GEMINI_API_KEY"}.get(prefix, "OPENAI_API_KEY")
    backend = OpenHandsCoderBackend(
        route_env=route_env_for_model(model, os.environ.get(key_var)),
        artifacts_root=tmp_path / "runs")
    result = backend.run(CoderTaskRequest(
        task_briefing=(
            "In hello.py, the add() function subtracts instead of "
            "adding. Fix it so it returns a + b."),
        repo_path=str(toy_repo), model_alias=model,
        budget=CoderBudget(tokens=200_000, usd=3.0,
                           wall_clock_seconds=900.0)))
    assert result.status == "done", result.summary
    assert "return a + b" in result.diff
    assert _git(toy_repo, "status", "--porcelain") == ""


# Keep asyncio imported for environments where the asyncio marker
# plugin resolves it lazily.
_ = asyncio
