"""S22 / W-9: the Workflows contract.

Pins what the slice spec promises for the engine surface:

* a workflow is {id, name, description, steps: [{title, prompt}],
  created_at, updated_at}; save creates by id absence and updates
  by id presence, and an update naming an unknown id is an honest
  refusal (None from the store, success=False on the wire) —
  never a silent create-instead;
* validation refuses, with a reason: a blank/missing name, zero
  steps, a step with an empty prompt (a workflow may not smuggle
  in a prompt-less task start), a non-object step;
* a step with a blank title takes its title from its prompt;
* delete returns the removed workflow, and deleting a missing id
  is success=False with "nothing was deleted" — never a fake
  success;
* definitions survive a fresh store over the same file (durable
  across restart);
* the wire handlers answer in the pinned shapes
  (WORKFLOW_LIST / SAVE / DELETE / RUN / RUN_STOP / RUN_LIST).

The store is a tmp JSON file; handler tests follow the
test_s22_contract_calendar.py fake-conn pattern, with the runner
seams patched on the runner module.
"""
import json

import pytest

from aether_common.contracts import EventType
from aether_engine.routes import ws_workflows
from aether_engine.workflows import runner
from aether_engine.workflows.store import (
    WorkflowStore,
    public_run,
    validate_workflow,
)


@pytest.fixture()
def store(tmp_path):
    return WorkflowStore(tmp_path / "workflows.json")


def _workflow(**overrides):
    data = {
        "name": "Morning research",
        "description": "News, then a summary",
        "steps": [
            {"title": "Gather", "prompt": "Collect today's headlines"},
            {"title": "Summarise", "prompt": "Summarise what you found"},
        ],
    }
    data.update(overrides)
    return data


# ── Store: CRUD shapes ────────────────────────────────────────


def test_save_creates_with_the_contract_shape(store):
    saved = store.save_workflow(_workflow())
    assert saved["id"]
    assert saved["name"] == "Morning research"
    assert saved["description"] == "News, then a summary"
    assert saved["steps"] == [
        {"title": "Gather", "prompt": "Collect today's headlines"},
        {"title": "Summarise", "prompt": "Summarise what you found"},
    ]
    assert saved["created_at"] > 0
    assert saved["updated_at"] == saved["created_at"]


def test_save_updates_by_id_and_keeps_created_at(store):
    saved = store.save_workflow(_workflow())
    updated = store.save_workflow(
        _workflow(id=saved["id"], name="Evening research"))
    assert updated["id"] == saved["id"]
    assert updated["name"] == "Evening research"
    assert updated["created_at"] == saved["created_at"]
    assert updated["updated_at"] >= saved["updated_at"]
    assert store.list_workflows()[0]["name"] == "Evening research"


def test_save_with_an_unknown_id_is_refused_not_created(store):
    result = store.save_workflow(_workflow(id="no-such-id"))
    assert result is None
    assert store.list_workflows() == []


def test_list_is_creation_order(store):
    store.save_workflow(_workflow(name="First"))
    store.save_workflow(_workflow(name="Second"))
    assert [w["name"] for w in store.list_workflows()] == [
        "First", "Second"]


def test_delete_returns_the_removed_workflow(store):
    saved = store.save_workflow(_workflow())
    removed = store.delete_workflow(saved["id"])
    assert removed["id"] == saved["id"]
    assert store.get_workflow(saved["id"]) is None


def test_delete_a_missing_id_returns_none(store):
    assert store.delete_workflow("no-such-id") is None


def test_definitions_survive_a_fresh_store_over_the_same_file(tmp_path):
    path = tmp_path / "workflows.json"
    WorkflowStore(path).save_workflow(_workflow())
    reopened = WorkflowStore(path)
    workflows = reopened.list_workflows()
    assert len(workflows) == 1
    assert workflows[0]["name"] == "Morning research"
    assert len(workflows[0]["steps"]) == 2


def test_a_corrupt_store_file_is_an_honest_error(tmp_path):
    path = tmp_path / "workflows.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be"):
        WorkflowStore(path)


# ── Store: validation refusals ────────────────────────────────


@pytest.mark.parametrize("data", [
    {"steps": [{"prompt": "Do it"}]},                    # no name
    {"name": "   ", "steps": [{"prompt": "Do it"}]},     # blank name
    {"name": "X"},                                       # no steps
    {"name": "X", "steps": []},                          # zero steps
    {"name": "X", "steps": [{"title": "T", "prompt": "  "}]},  # empty prompt
    {"name": "X", "steps": [{"title": "T"}]},            # missing prompt
    {"name": "X", "steps": ["just a string"]},           # non-object step
    {"name": "X", "steps": [{"prompt": "ok"}, {"prompt": ""}]},  # 2nd empty
    "not a dict",
])
def test_validation_refusals(store, data):
    # Wrong-typed input refuses with TypeError, bad values with
    # ValueError; both are honest refusals that store nothing.
    with pytest.raises((ValueError, TypeError)):
        store.save_workflow(data)
    assert store.list_workflows() == []


def test_validation_error_names_the_empty_step():
    with pytest.raises(ValueError, match="step 2 has an empty prompt"):
        validate_workflow({"name": "X", "steps": [
            {"prompt": "fine"}, {"prompt": " "}]})


def test_a_blank_step_title_defaults_from_the_prompt(store):
    saved = store.save_workflow({
        "name": "Titles",
        "steps": [
            {"prompt": "First line is the title\nwith more detail"},
            {"title": "  ", "prompt": "Short"},
        ],
    })
    assert saved["steps"][0]["title"] == "First line is the title"
    assert saved["steps"][1]["title"] == "Short"


def test_a_long_prompt_title_is_truncated(store):
    saved = store.save_workflow({
        "name": "Titles",
        "steps": [{"prompt": "x" * 200}],
    })
    title = saved["steps"][0]["title"]
    assert len(title) <= 60
    assert title.endswith("…")


# ── Run records ───────────────────────────────────────────────


def _run_record(run_id="run-1", status="running"):
    return {
        "run_id": run_id,
        "workflow_id": "wf-1",
        "session_id": "sess-1",
        "status": status,
        "current_step": 0,
        "steps": [{"title": "Gather", "status": "running",
                   "task_id": None}],
    }


def test_run_records_round_trip_and_project_to_the_wire_shape(store):
    store.create_run(_run_record())
    record = store.get_run("run-1")
    assert record["status"] == "running"
    wire = public_run(record)
    assert set(wire) == {
        "run_id", "workflow_id", "session_id", "status",
        "current_step", "steps"}
    assert wire["steps"] == [
        {"title": "Gather", "status": "running", "task_id": None}]
    store.update_run("run-1", status="completed")
    assert store.get_run("run-1")["status"] == "completed"
    assert store.active_runs() == []


def test_update_a_missing_run_returns_none(store):
    assert store.update_run("nope", status="failed") is None


# ── Wire handlers ─────────────────────────────────────────────


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _FakeConn:
    def __init__(self):
        self.ws = _FakeWs()


class _FakeMsg:
    def __init__(self, payload=None):
        self.request_id = "req-1"
        self.payload = payload or {}


@pytest.fixture()
def wire(monkeypatch, store):
    monkeypatch.setattr(
        ws_workflows, "get_workflow_store", lambda: store)
    return _FakeConn()


def _frames(conn):
    return [json.loads(text) for text in conn.ws.sent]


async def test_list_handler_answers_the_pinned_shape(wire, store):
    store.save_workflow(_workflow())
    await ws_workflows.handle_workflow_list_request(wire, _FakeMsg())
    frame = _frames(wire)[0]
    assert frame["type"] == EventType.WORKFLOW_LIST_RESPONSE.value
    assert frame["payload"]["workflows"][0]["name"] == "Morning research"
    assert "error" not in frame["payload"]


async def test_save_handler_creates_and_replies(wire):
    await ws_workflows.handle_workflow_save_request(
        wire, _FakeMsg({"workflow": _workflow()}))
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is True
    assert payload["workflow"]["name"] == "Morning research"
    assert payload["error"] is None


async def test_save_handler_refuses_an_invalid_workflow(wire):
    await ws_workflows.handle_workflow_save_request(
        wire, _FakeMsg({"workflow": {"name": "", "steps": []}}))
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is False
    assert payload["workflow"] is None
    assert "name" in payload["error"]


async def test_save_handler_refuses_an_unknown_id(wire):
    await ws_workflows.handle_workflow_save_request(
        wire, _FakeMsg({"workflow": _workflow(id="ghost")}))
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is False
    assert "nothing was saved" in payload["error"]


async def test_delete_handler_and_missing_id_honesty(wire, store):
    saved = store.save_workflow(_workflow())
    await ws_workflows.handle_workflow_delete_request(
        wire, _FakeMsg({"workflow_id": saved["id"]}))
    assert _frames(wire)[0]["payload"] == {
        "success": True, "error": None}
    await ws_workflows.handle_workflow_delete_request(
        wire, _FakeMsg({"workflow_id": saved["id"]}))
    payload = _frames(wire)[1]["payload"]
    assert payload["success"] is False
    assert "nothing was deleted" in payload["error"]


async def test_delete_handler_requires_an_id(wire):
    await ws_workflows.handle_workflow_delete_request(wire, _FakeMsg())
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is False
    assert "workflow_id is required" in payload["error"]


async def test_run_handler_refuses_an_unknown_workflow(wire):
    await ws_workflows.handle_workflow_run_request(
        wire, _FakeMsg({"workflow_id": "ghost"}))
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is False
    assert payload["run"] is None
    assert "nothing was started" in payload["error"]


async def test_run_handler_starts_and_replies_with_the_run(
        wire, store, monkeypatch):
    saved = store.save_workflow(_workflow())

    async def fake_start_run(conn, store_, workflow):
        record = _run_record(run_id="run-9")
        record["workflow_id"] = workflow["id"]
        store_.create_run(record)
        return store_.get_run("run-9")

    monkeypatch.setattr(runner, "start_run", fake_start_run)
    await ws_workflows.handle_workflow_run_request(
        wire, _FakeMsg({"workflow_id": saved["id"]}))
    frames = _frames(wire)
    payload = frames[0]["payload"]
    assert payload["success"] is True
    assert payload["run"]["run_id"] == "run-9"
    assert payload["run"]["workflow_id"] == saved["id"]
    # The initial WORKFLOW_RUN_STATUS follows the reply.
    assert frames[1]["type"] == EventType.WORKFLOW_RUN_STATUS.value
    assert frames[1]["payload"]["run_id"] == "run-9"


async def test_run_stop_handler_reports_a_non_live_run(wire, store):
    store.create_run(_run_record(status="completed"))
    await ws_workflows.handle_workflow_run_stop_request(
        wire, _FakeMsg({"run_id": "run-1"}))
    payload = _frames(wire)[0]["payload"]
    assert payload["success"] is False
    assert "nothing was stopped" in payload["error"]


async def test_run_list_handler_answers_runs_newest_first(wire, store):
    old = _run_record(run_id="run-old", status="completed")
    old["created_at"] = 1000.0
    new = _run_record(run_id="run-new", status="failed")
    new["created_at"] = 2000.0
    store.create_run(old)
    store.create_run(new)
    await ws_workflows.handle_workflow_run_list_request(wire, _FakeMsg())
    payload = _frames(wire)[0]["payload"]
    assert [r["run_id"] for r in payload["runs"]] == [
        "run-new", "run-old"]
