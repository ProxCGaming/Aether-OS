"""P4a — mini-SWE coder backend: wrapper lifecycle, containment,
budgets, the recorded-fixture child run, and the coder-node flag.

No live model is called anywhere in this file:

- wrapper tests drive the subprocess lifecycle with a FAKE CHILD
  script honouring the wrapper's argv contract (the wrapper's
  ``child_argv`` seam exists for exactly this);
- the real child entry point is exercised in-process with
  ``LitellmModel._query`` replaced by scripted responses — the
  recorded fixture: a fixed toy task whose expected diff shape is
  asserted exactly;
- coder-node tests substitute a stub backend at the node's
  construction seam.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.backends.coder_backend import (
    AttemptContext,
    CoderBudget,
    CoderTaskRequest,
    CoderTaskResult,
)
from aether_engine.langgraph.backends.coder_miniswe import (
    MiniSweCoderBackend,
    _task_text,
    route_env_for_model,
    scrub_environment,
)

# ---------------------------------------------------------------------------
# Toy repo + fake child fixtures
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


FAKE_CHILD = r'''
import json, os, subprocess, sys, time
from pathlib import Path

req = json.loads(Path(sys.argv[-2]).read_text())
mode = os.environ.get("FAKE_CHILD_MODE", "fix")
cwd = Path.cwd()

def note(text):
    with open(req["progress_path"], "a") as fh:
        fh.write(json.dumps({"role": "assistant", "content": text}) + "\n")

def finish(result, rc=0):
    Path(sys.argv[-1]).write_text(json.dumps(result))
    sys.exit(rc)

if mode == "fix":
    (cwd / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    (cwd / "notes.txt").write_text("done\n")
    probe = {
        "parent_secret_present": "PARENT_SECRET_API_KEY" in os.environ,
        "route_present": os.environ.get("GEMINI_API_KEY") == "route-key",
        "mswea_cfg_is_dir": os.path.isdir(os.environ.get("MSWEA_GLOBAL_CONFIG_DIR", "")),
    }
    (cwd / "env_probe.json").write_text(json.dumps(probe))
    note("fixed the bug")
    note("submitting")
    finish({"exit_status": "Submitted", "message": "Fixed add()",
            "cost": 0.25, "tokens": 1234, "n_calls": 2})
elif mode == "limits":
    finish({"exit_status": "LimitsExceeded", "message": "cost limit hit",
            "cost": 3.0, "tokens": 999, "n_calls": 5})
elif mode == "error":
    finish({"exit_status": "Error", "message": "kaboom",
            "cost": 0.0, "tokens": 0, "n_calls": 0}, rc=3)
elif mode == "sleep":
    note("started")
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
    script = tmp_path / "fake_child.py"
    script.write_text(FAKE_CHILD)
    return script


def _backend(fake_child: Path, artifacts: Path, **kw) -> MiniSweCoderBackend:
    import sys
    return MiniSweCoderBackend(
        artifacts_root=artifacts,
        child_argv=[sys.executable, str(fake_child)], **kw)


def _request(repo: Path, **kw) -> CoderTaskRequest:
    kw.setdefault("task_briefing", "Fix the sign bug in add()")
    kw.setdefault("model_alias", "gemini/gemini-2.5-flash")
    return CoderTaskRequest(repo_path=str(repo), **kw)


# ---------------------------------------------------------------------------
# Wrapper lifecycle (fake child)
# ---------------------------------------------------------------------------


class TestWrapperLifecycle:
    def test_done_run_returns_diff_record_and_clean_live_tree(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "fix")
        monkeypatch.setenv("PARENT_SECRET_API_KEY", "should-not-leak")
        backend = _backend(fake_child, tmp_path / "runs",
                           route_env={"GEMINI_API_KEY": "route-key"})
        result = backend.run(_request(toy_repo))

        assert result.status == "done"
        assert result.backend == "miniswe"
        assert result.summary == "Fixed add()"
        assert result.cost_actual == 0.25
        assert result.tokens_actual == 1234
        # Every write the child made is visible in the diff.
        assert "return a + b" in result.diff
        assert "notes.txt" in result.diff
        assert sorted(result.files_touched) == [
            "env_probe.json", "hello.py", "notes.txt"]
        # The child's env: parent secret scrubbed, route injected,
        # the library's global-config dir redirected to the run dir.
        assert '"parent_secret_present": false' in result.diff
        assert '"route_present": true' in result.diff
        assert '"mswea_cfg_is_dir": true' in result.diff
        # Live tree untouched, worktree removed.
        assert _git(toy_repo, "status", "--porcelain") == ""
        assert "toy-wt" not in _git(toy_repo, "worktree", "list")
        assert (toy_repo / "hello.py").read_text().endswith("return a - b\n")
        # Transcript: complete, with the child's recorded messages.
        transcript = json.loads(Path(result.transcript_ref).read_text())
        assert transcript["complete"] is True
        assert transcript["exit_status"] == "Submitted"
        assert [m["content"] for m in transcript["messages"]] == [
            "fixed the bug", "submitting"]

    def test_limits_exceeded_maps_to_budget_stopped(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "limits")
        result = _backend(fake_child, tmp_path / "runs").run(_request(toy_repo))
        assert result.status == "budget-stopped"
        assert result.cost_actual == 3.0

    def test_child_error_maps_to_failed(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "error")
        result = _backend(fake_child, tmp_path / "runs").run(_request(toy_repo))
        assert result.status == "failed"
        assert "kaboom" in result.summary

    def test_wall_clock_budget_kills_a_hung_child(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "sleep")
        backend = _backend(fake_child, tmp_path / "runs", kill_grace_s=0.5)
        started = time.monotonic()
        result = backend.run(_request(
            toy_repo, budget=CoderBudget(wall_clock_seconds=1.0)))
        elapsed = time.monotonic() - started
        assert result.status == "budget-stopped"
        assert elapsed < 20  # the 60s child was killed, not awaited
        transcript = json.loads(Path(result.transcript_ref).read_text())
        assert transcript["complete"] is False
        # …but the progress transcript still holds what finished.
        assert [m["content"] for m in transcript["messages"]] == ["started"]

    def test_cancel_kills_the_child(
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "sleep")
        backend = _backend(fake_child, tmp_path / "runs")
        outcome: dict = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault(
                "result", backend.run(_request(toy_repo))))
        thread.start()
        # Wait until the child is actually running (its progress
        # line lands), then cancel.
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
            self, toy_repo, fake_child, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD_MODE", "escape")
        result = _backend(fake_child, tmp_path / "runs").run(_request(toy_repo))
        assert result.status == "failed"
        assert "CONTAINMENT BREACH" in result.summary
        assert (toy_repo / "breach.txt").exists()  # the evidence stays
        (toy_repo / "breach.txt").unlink()

    def test_unusable_repo_fails_cleanly_with_a_transcript(
            self, fake_child, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = _backend(fake_child, tmp_path / "runs").run(_request(plain))
        assert result.status == "failed"
        assert "not usable" in result.summary
        assert Path(result.transcript_ref).exists()


# ---------------------------------------------------------------------------
# Env scrub + route mapping (units)
# ---------------------------------------------------------------------------


class TestEnvRules:
    def test_scrub_drops_secret_shapes_keeps_the_rest(self):
        env = {
            "PATH": "/usr/bin", "HOME": "/home/x", "LANG": "en_US",
            "OPENAI_API_KEY": "k", "MY_SESSION_TOKEN": "t",
            "DB_PASSWORD": "p", "APP_SECRET": "s",
            "AWS_CREDENTIALS": "c", "NORMAL_SETTING": "1",
        }
        kept = scrub_environment(env)
        assert kept == {"PATH": "/usr/bin", "HOME": "/home/x",
                        "LANG": "en_US", "NORMAL_SETTING": "1"}

    def test_route_env_maps_prefixes(self):
        assert route_env_for_model("gemini/gemini-2.5-flash", "k") == {
            "GEMINI_API_KEY": "k"}
        assert route_env_for_model("anthropic/claude-sonnet", "k") == {
            "ANTHROPIC_API_KEY": "k"}
        assert route_env_for_model("gpt-4o", "k") == {"OPENAI_API_KEY": "k"}
        assert route_env_for_model("ollama/qwen3:8b", "k") == {}
        assert route_env_for_model("gemini/gemini-2.5-flash", "<redacted>") == {}
        assert route_env_for_model("gemini/gemini-2.5-flash", None) == {}

    def test_task_text_carries_attempt_context(self):
        req = CoderTaskRequest(
            task_briefing="Fix it", repo_path="/r", model_alias="m",
            attempt_context=AttemptContext(
                prior_diff="-a\n+b", rejection_reason="tests fail"))
        text = _task_text(req)
        assert "Fix it" in text and "tests fail" in text
        assert "-a\n+b" in text and "NOT applied" in text
        plain = _task_text(CoderTaskRequest(
            task_briefing="Fix it", repo_path="/r", model_alias="m"))
        assert plain == "Fix it"


# ---------------------------------------------------------------------------
# The real child, recorded fixture (scripted model, no live call)
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, total: int):
        self.total_tokens = total
        self.prompt_tokens = total
        self.completion_tokens = 0


class _FakeResponse:
    def __init__(self, content: str, tokens: int):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})()})()]
        self.usage = _FakeUsage(tokens)

    def model_dump(self):
        return {}


class TestRecordedFixtureChild:
    def _run_child(self, toy_repo, tmp_path, monkeypatch, script, budget):
        from minisweagent.models.litellm_model import LitellmModel

        from aether_engine.langgraph.backends import coder_miniswe

        calls = {"n": 0}

        def fake_query(self, messages, **kwargs):
            idx = min(calls["n"], len(script) - 1)
            calls["n"] += 1
            content, tokens = script[idx]
            return _FakeResponse(content, tokens)

        monkeypatch.setattr(LitellmModel, "_query", fake_query)

        wt = tmp_path / "wt"
        _git(toy_repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        request_path = run_dir / "request.json"
        result_path = run_dir / "result.json"
        request_path.write_text(json.dumps({
            "task": "Fix the sign bug in add() in hello.py",
            "model_name": "scripted/fake",
            "model_kwargs": {},
            "budget": budget,
            "worktree": str(wt),
            "progress_path": str(run_dir / "progress.jsonl"),
            "per_command_timeout_s": 30,
        }))
        rc = coder_miniswe._child_main(
            ["coder_miniswe", str(request_path), str(result_path)])
        assert rc == 0
        return (json.loads(result_path.read_text()),
                (run_dir / "progress.jsonl").read_text(), wt)

    def test_fixed_task_produces_the_expected_diff(
            self, toy_repo, tmp_path, monkeypatch):
        script = [
            ("```bash\nsed -i 's/return a - b/return a + b/' hello.py\n```", 100),
            (("```bash\nprintf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
              "Fixed add() sign bug\\n'\n```"), 50),
        ]
        result, progress, wt = self._run_child(
            toy_repo, tmp_path, monkeypatch, script,
            {"tokens": None, "usd": None, "wall_clock_seconds": None})
        assert result["exit_status"] == "Submitted"
        assert result["message"].strip() == "Fixed add() sign bug"
        assert result["tokens"] == 150
        assert result["n_calls"] == 2
        # The diff shape the fixture promises: exactly the sign fix.
        _git(wt, "add", "-A")
        diff = _git(wt, "diff", "--cached")
        assert "-    return a - b" in diff
        assert "+    return a + b" in diff
        # Transcript progress: system + task + both rounds recorded.
        lines = [json.loads(l) for l in progress.splitlines() if l.strip()]
        assert len(lines) >= 5
        assert lines[0]["role"] == "system"

    def test_token_budget_stops_the_child(
            self, toy_repo, tmp_path, monkeypatch):
        script = [
            ("```bash\ntrue\n```", 100),
            ("```bash\ntrue\n```", 100),
        ]
        result, _progress, _wt = self._run_child(
            toy_repo, tmp_path, monkeypatch, script,
            {"tokens": 50, "usd": None, "wall_clock_seconds": None})
        assert result["exit_status"] == "LimitsExceeded"
        assert result["tokens"] == 100


# ---------------------------------------------------------------------------
# Coder-node integration (flag on/off, stub backend)
# ---------------------------------------------------------------------------


class _StubBackend:
    name = "miniswe"

    def __init__(self, result: CoderTaskResult):
        self._result = result
        self.requests: list[CoderTaskRequest] = []

    def run(self, request: CoderTaskRequest) -> CoderTaskResult:
        self.requests.append(request)
        return self._result


def _state(**over) -> dict:
    state = {
        "messages": [{"role": "user", "content": "Fix the sign bug"}],
        "task_id": "task-p4a",
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
    async def test_native_is_the_default_and_unchanged(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        seen: dict = {}

        async def sentinel(spec, state, config, *, save_artifact=None):
            seen.update(spec=spec, save_artifact=save_artifact)
            return {"sentinel": True}

        monkeypatch.setattr(coder, "run_specialist", sentinel)
        out = await coder.coder_node(_state(), _config(UserConfig()))
        assert out == {"sentinel": True}
        assert seen["spec"] is coder.CODER_SPEC
        # The patch seam survives: the node's own module global.
        assert seen["save_artifact"] is coder.save_artifact

    @pytest.mark.asyncio
    async def test_miniswe_flag_routes_to_the_backend(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend(CoderTaskResult(
            status="done", diff="--- a/hello.py\n+++ b/hello.py\n",
            summary="Fixed add()", files_touched=["hello.py"],
            cost_actual=0.5, transcript_ref="/runs/x/transcript.json",
            backend="miniswe", tokens_actual=42))
        monkeypatch.setattr(
            coder, "_build_miniswe_backend",
            lambda config, provider: (stub, "gemini/gemini-2.5-flash"))
        monkeypatch.setattr(
            coder, "save_artifact", lambda *a: "/artifacts/coder_output.txt")

        out = await coder.coder_node(
            _state(), _config(UserConfig(coder_backend="miniswe")))

        assert len(stub.requests) == 1
        request = stub.requests[0]
        assert request.repo_path == "/tmp/some-repo"
        assert request.model_alias == "gemini/gemini-2.5-flash"
        assert "Fix the sign bug" in request.task_briefing
        assert out["current_phase"] == "coding"
        content = out["messages"][0]["content"]
        assert "Fixed add()" in content
        assert "Proposed diff" in content and "hello.py" in content
        entry = out["delegation_log"][0]
        assert entry["agent"] == "coder"
        assert entry["backend"] == "miniswe"
        assert entry["status"] == "done"
        assert entry["cost_actual"] == 0.5
        assert entry["transcript_ref"] == "/runs/x/transcript.json"
        assert "decision" not in entry  # existing log readers skip it
        assert out["artifact_paths"] == {
            "coder_output": "/artifacts/coder_output.txt"}

    @pytest.mark.asyncio
    async def test_plan_mode_never_uses_the_backend(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        async def sentinel(spec, state, config, *, save_artifact=None):
            return {"sentinel": True}

        monkeypatch.setattr(coder, "run_specialist", sentinel)
        monkeypatch.setattr(
            coder, "_build_miniswe_backend",
            lambda config, provider: (_ for _ in ()).throw(
                AssertionError("backend built in plan mode")))
        out = await coder.coder_node(
            _state(plan_mode=True),
            _config(UserConfig(coder_backend="miniswe")))
        assert out == {"sentinel": True}

    def test_config_flag_defaults_and_validation(self):
        assert UserConfig().coder_backend == "native"
        assert UserConfig.from_dict(
            {"coder_backend": "miniswe"}).coder_backend == "miniswe"
        assert UserConfig.from_dict(
            {"coder_backend": "bogus"}).coder_backend == "native"
        assert UserConfig(coder_backend="miniswe").to_dict()[
            "coder_backend"] == "miniswe"


def test_backend_satisfies_the_protocol():
    from aether_engine.langgraph.backends.coder_backend import CoderBackend
    assert isinstance(MiniSweCoderBackend(), CoderBackend)


# ---------------------------------------------------------------------------
# Opt-in live smoke (plan step 3) — a real model against the toy
# repo. Skipped unless explicitly enabled:
#   AETHER_P4A_LIVE_SMOKE=1 AETHER_P4A_SMOKE_MODEL=gemini/gemini-2.5-flash
# with the route's API key present in the environment.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AETHER_P4A_LIVE_SMOKE") != "1",
    reason="opt-in live smoke: set AETHER_P4A_LIVE_SMOKE=1")
def test_live_smoke(toy_repo, tmp_path):
    from aether_engine.langgraph.backends.coder_miniswe import (
        route_env_for_model,
    )

    model = os.environ.get(
        "AETHER_P4A_SMOKE_MODEL", "gemini/gemini-2.5-flash")
    prefix = model.split("/", 1)[0] if "/" in model else "openai"
    key_var = {"gemini": "GEMINI_API_KEY"}.get(prefix, "OPENAI_API_KEY")
    backend = MiniSweCoderBackend(
        route_env=route_env_for_model(model, os.environ.get(key_var)),
        artifacts_root=tmp_path / "runs")
    result = backend.run(CoderTaskRequest(
        task_briefing=(
            "In hello.py, the add() function subtracts instead of "
            "adding. Fix it so it returns a + b."),
        repo_path=str(toy_repo), model_alias=model,
        budget=CoderBudget(tokens=100_000, usd=1.0,
                           wall_clock_seconds=600.0)))
    assert result.status == "done", result.summary
    assert "return a + b" in result.diff
    assert _git(toy_repo, "status", "--porcelain") == ""


# Keep asyncio imported for environments where the asyncio marker
# plugin resolves it lazily.
_ = asyncio
