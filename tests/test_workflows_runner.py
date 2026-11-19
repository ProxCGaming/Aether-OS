"""W-9 runner tests: order, chaining, failure, approval, stop,
restart.

The runner drives steps through the ordinary START_TASK path, so
these tests fake that path at its patch-doctrine home
(``aether_engine.routes.websocket.start_task_message``) with a
scripted world: each launch records its prompt and installs a
fake pump on the connection; the pump writes the same evidence
the real pump writes — the task's audit transitions (read back
through the runner's ``read_audit_records`` seam) and, on
completion, the assistant message in the session (read back
through the runner's ``session_store`` seam). Session creation
is faked at the runner's ``_create_bound_session`` seam.
"""
import asyncio
from types import SimpleNamespace

import pytest

from aether_engine.workflows import runner
from aether_engine.workflows.store import WorkflowStore


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _FakeConn:
    def __init__(self):
        self.ws = _FakeWs()
        self.task_runner = None
        self.task_start_tail = None
        self.active_task_id = None
        self.background_tasks = set()

    def spawn_background(self, coro):
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task


class _World:
    """The scripted engine the runner drives."""

    def __init__(self):
        self.sessions = {}       # session_id -> {"messages": [...]}
        self.transitions = {}    # task_id -> [{"to_state": str}]
        self.launches = []       # [{"prompt", "session_id", "task_id"}]
        self.behaviors = []      # one async fn per launch, in order
        self.pending_approvals = []

    # — the seams' fake implementations —

    async def create_session(self, title):
        session_id = f"sess-{len(self.sessions) + 1}"
        self.sessions[session_id] = {"messages": []}
        return session_id, None

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def read_audit_records(self, task_id):
        return {
            "transitions": list(self.transitions.get(task_id, [])),
            "approvals": [], "events": [],
        }

    async def get_pending_approvals(self):
        return list(self.pending_approvals)

    async def start_task_message(self, conn, msg, req_id, previous):
        if previous is not None:
            try:
                await previous
            except Exception:  # noqa: BLE001, S110 — mirror of the real launch chain: an earlier launch's failure is its own to answer
                pass
        prompt = msg.payload["prompt"]
        session_id = msg.payload["session_id"]
        task_id = f"task-{len(self.launches) + 1}"
        self.launches.append({
            "prompt": prompt, "session_id": session_id,
            "task_id": task_id})
        behavior = self.behaviors.pop(0)

        async def pump():
            try:
                await behavior(self, conn, task_id, session_id)
            finally:
                if conn.active_task_id == task_id:
                    conn.active_task_id = None

        conn.task_runner = asyncio.create_task(pump())

    # — behaviour helpers —

    def complete_with(self, text, delay=0.05):
        async def behavior(world, conn, task_id, session_id):
            conn.active_task_id = task_id
            await asyncio.sleep(delay)
            world.sessions[session_id]["messages"].append(
                {"role": "assistant", "content": text})
            world.transitions[task_id] = [{"to_state": "SUCCEEDED"}]
        return behavior

    def fail(self, delay=0.05):
        async def behavior(world, conn, task_id, session_id):
            conn.active_task_id = task_id
            await asyncio.sleep(delay)
            world.transitions[task_id] = [{"to_state": "FAILED"}]
        return behavior

    def hang(self):
        async def behavior(world, conn, task_id, session_id):
            conn.active_task_id = task_id
            await asyncio.sleep(60)
        return behavior

    def pause_for_approval(self, delay=0.05):
        async def behavior(world, conn, task_id, session_id):
            conn.active_task_id = task_id
            await asyncio.sleep(delay)
            world.transitions[task_id] = [
                {"to_state": "PENDING_APPROVAL"}]
            world.pending_approvals = [{"thread_id": session_id}]
        return behavior


@pytest.fixture()
def world(monkeypatch):
    w = _World()
    from aether_engine.routes import websocket as ws_module

    monkeypatch.setattr(ws_module, "start_task_message",
                        w.start_task_message)
    monkeypatch.setattr(ws_module, "get_pending_approvals",
                        w.get_pending_approvals)
    monkeypatch.setattr(runner, "_create_bound_session", w.create_session)
    monkeypatch.setattr(runner, "read_audit_records", w.read_audit_records)
    monkeypatch.setattr(
        runner, "session_store",
        SimpleNamespace(get_session=w.get_session))
    return w


@pytest.fixture()
def store(tmp_path):
    return WorkflowStore(tmp_path / "workflows.json")


def _workflow(store, prompts, name="Morning research"):
    return store.save_workflow({
        "name": name,
        "steps": [{"prompt": p} for p in prompts],
    })


async def _wait_for(predicate, timeout=5.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


# ── Chaining units ────────────────────────────────────────────


def test_excerpt_is_capped_and_the_trim_is_marked():
    text = "a" * (runner.PREVIOUS_STEP_EXCERPT_CHARS + 500)
    excerpt = runner.excerpt_previous_result(text)
    assert excerpt.startswith("a" * runner.PREVIOUS_STEP_EXCERPT_CHARS)
    assert "a" * (runner.PREVIOUS_STEP_EXCERPT_CHARS + 1) not in excerpt
    assert "trimmed" in excerpt


def test_excerpt_under_the_cap_is_verbatim():
    assert runner.excerpt_previous_result("  short result  ") == "short result"
    assert runner.excerpt_previous_result(None) == ""
    assert runner.excerpt_previous_result("") == ""


def test_build_step_prompt_carries_the_previous_result_first():
    prompt = runner.build_step_prompt(
        "Summarise it", "Gather", "the headlines")
    assert prompt == (
        'Result of the previous step ("Gather"):\n'
        "the headlines\n\nSummarise it")


def test_build_step_prompt_with_no_previous_text_is_the_prompt():
    assert runner.build_step_prompt("Do it", "Gather", "") == "Do it"
    assert runner.build_step_prompt("Do it", "Gather", None) == "Do it"


# ── The driver ────────────────────────────────────────────────


async def test_steps_run_in_order_and_chain_results(world, store):
    workflow = _workflow(store, ["Gather headlines", "Summarise them",
                                 "Draft a post"])
    world.behaviors = [
        world.complete_with("headlines text"),
        world.complete_with("summary text"),
        world.complete_with("post text"),
    ]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    live = runner.get_live_run(record["run_id"])
    await asyncio.wait_for(live.driver, 5)

    assert [launch["prompt"] for launch in world.launches] == [
        "Gather headlines",
        ('Result of the previous step ("Gather headlines"):\n'
         "headlines text\n\nSummarise them"),
        ('Result of the previous step ("Summarise them"):\n'
         "summary text\n\nDraft a post"),
    ]
    final = store.get_run(record["run_id"])
    assert final["status"] == "completed"
    assert [s["status"] for s in final["steps"]] == [
        "completed", "completed", "completed"]
    assert [s["task_id"] for s in final["steps"]] == [
        "task-1", "task-2", "task-3"]
    assert runner.get_live_run(record["run_id"]) is None


async def test_the_chain_carries_a_bounded_excerpt(world, store):
    workflow = _workflow(store, ["Gather", "Summarise"])
    big = "x" * 2500
    world.behaviors = [
        world.complete_with(big),
        world.complete_with("done"),
    ]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    live = runner.get_live_run(record["run_id"])
    await asyncio.wait_for(live.driver, 5)

    second_prompt = world.launches[1]["prompt"]
    cap = runner.PREVIOUS_STEP_EXCERPT_CHARS
    assert "x" * cap in second_prompt
    assert "x" * (cap + 1) not in second_prompt
    assert "trimmed" in second_prompt
    assert second_prompt.endswith("Summarise")


async def test_a_failed_step_stops_the_run(world, store):
    workflow = _workflow(store, ["One", "Two", "Three"])
    world.behaviors = [
        world.complete_with("first result"),
        world.fail(),
        world.complete_with("never reached"),
    ]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    live = runner.get_live_run(record["run_id"])
    await asyncio.wait_for(live.driver, 5)

    # The third step was never started.
    assert len(world.launches) == 2
    final = store.get_run(record["run_id"])
    assert final["status"] == "failed"
    assert [s["status"] for s in final["steps"]] == [
        "completed", "failed", "skipped"]


async def test_an_approval_pause_pauses_then_continues(world, store):
    workflow = _workflow(store, ["Risky step", "Follow up"])
    world.behaviors = [
        world.pause_for_approval(),
        world.complete_with("follow-up result"),
    ]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    run_id = record["run_id"]

    await _wait_for(
        lambda: store.get_run(run_id)["status"] == "awaiting_approval")
    # Nothing further starts while the task waits on the owner.
    assert len(world.launches) == 1

    # The owner answers; the ordinary approval flow resumes the
    # task — a new pump on the same connection, same task id —
    # and it completes with the step's result.
    async def resume_pump():
        task_id = world.launches[0]["task_id"]
        session_id = world.launches[0]["session_id"]
        conn.active_task_id = task_id
        await asyncio.sleep(0.02)
        world.pending_approvals = []
        world.sessions[session_id]["messages"].append(
            {"role": "assistant", "content": "approved result"})
        world.transitions[task_id].append({"to_state": "SUCCEEDED"})
        conn.active_task_id = None

    conn.task_runner = asyncio.create_task(resume_pump())
    live = runner.get_live_run(run_id)
    await asyncio.wait_for(live.driver, 5)

    assert len(world.launches) == 2
    assert "approved result" in world.launches[1]["prompt"]
    final = store.get_run(run_id)
    assert final["status"] == "completed"
    assert [s["status"] for s in final["steps"]] == [
        "completed", "completed"]


async def test_stop_cancels_the_step_and_skips_the_rest(world, store):
    workflow = _workflow(store, ["Long step", "Later step"])
    world.behaviors = [world.hang(), world.complete_with("never")]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    run_id = record["run_id"]
    await _wait_for(lambda: len(world.launches) == 1)

    stopped, error = runner.stop_run(store, run_id)
    assert (stopped, error) == (True, None)
    live = runner.get_live_run(run_id)
    await asyncio.wait_for(live.driver, 5)

    final = store.get_run(run_id)
    assert final["status"] == "stopped"
    assert [s["status"] for s in final["steps"]] == [
        "cancelled", "skipped"]
    assert len(world.launches) == 1


async def test_stop_of_a_finished_run_is_an_honest_error(world, store):
    workflow = _workflow(store, ["Only step"])
    world.behaviors = [world.complete_with("done")]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    live = runner.get_live_run(record["run_id"])
    await asyncio.wait_for(live.driver, 5)

    stopped, error = runner.stop_run(store, record["run_id"])
    assert stopped is False
    assert "nothing was stopped" in error
    stopped, error = runner.stop_run(store, "no-such-run")
    assert stopped is False
    assert "nothing was stopped" in error


# ── Concurrency refusals ──────────────────────────────────────


async def test_a_second_run_of_the_same_workflow_is_refused(world, store):
    workflow = _workflow(store, ["Long step"])
    world.behaviors = [world.hang()]
    conn = _FakeConn()
    record = await runner.start_run(conn, store, workflow)
    await _wait_for(lambda: len(world.launches) == 1)

    with pytest.raises(runner.WorkflowRunRefused, match="already running"):
        await runner.start_run(_FakeConn(), store, workflow)
    # A different workflow on the SAME connection is refused too:
    # the connection has one task slot.
    other = _workflow(store, ["Other"], name="Other workflow")
    with pytest.raises(runner.WorkflowRunRefused, match="already in progress"):
        await runner.start_run(conn, store, other)

    runner.stop_run(store, record["run_id"])
    live = runner.get_live_run(record["run_id"])
    await asyncio.wait_for(live.driver, 5)


# ── Restart discipline ────────────────────────────────────────


def test_a_stale_active_run_is_closed_as_interrupted(tmp_path):
    path = tmp_path / "workflows.json"
    first = WorkflowStore(path)
    first.create_run({
        "run_id": "run-stale", "workflow_id": "wf-1",
        "session_id": "sess-1", "status": "running",
        "current_step": 1,
        "steps": [
            {"title": "Done", "status": "completed", "task_id": "t-1"},
            {"title": "In flight", "status": "running",
             "task_id": "t-2"},
            {"title": "Waiting", "status": "pending", "task_id": None},
        ],
    })
    # A fresh store over the same file = a restarted engine.
    reopened = WorkflowStore(path)
    closed = runner.close_interrupted_runs(reopened)
    assert closed == ["run-stale"]
    record = reopened.get_run("run-stale")
    assert record["status"] == "interrupted"
    assert [s["status"] for s in record["steps"]] == [
        "completed", "cancelled", "pending"]
    # And it stays closed — a second pass finds nothing active.
    assert runner.close_interrupted_runs(reopened) == []


def test_a_live_run_is_never_closed_as_interrupted(store):
    store.create_run({
        "run_id": "run-live", "workflow_id": "wf-1",
        "session_id": "sess-1", "status": "running",
        "current_step": 0,
        "steps": [{"title": "Step", "status": "running",
                   "task_id": None}],
    })
    live = runner.LiveRun(
        run_id="run-live", workflow_id="wf-1", conn=_FakeConn())
    runner._LIVE_RUNS["run-live"] = live
    try:
        assert runner.close_interrupted_runs(store) == []
        assert store.get_run("run-live")["status"] == "running"
    finally:
        runner._LIVE_RUNS.pop("run-live", None)
