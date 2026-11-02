"""N05 — artifact auto-collection must observe actual filesystem effects.

The collection gate in ``execute_tool_node`` used to register artifacts
only for ``write_file``/``edit_file``. Files a task creates through the
shell (a script writing its output, a build producing a deliverable, an
export command) are real task deliverables (Grill Q8: files agents
write/edit, independent of the writing tool) and must reach the canvas
with the same task attribution — while merely-read files, deletions,
read-only tools, plan mode, and anything outside the task workspace
must never be collected.

Harness mirrors tests/test_artifact_store.py's auto-collection tests:
a fake in-process worker stands in for the sandbox subprocess and a
minimal registry carries just the tools under test.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts.store import ArtifactStore


def _build_harness(tmp_path, monkeypatch, outside_dir: Path):
    """Registry + fake worker whose shell commands have scripted effects."""
    from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk
    from aether_engine.workers import sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()
    calls: list[tuple[str, dict]] = []

    async def _fake_worker(tool_name, args, task_class="standard", audit_logger=None, task_classes=None):
        calls.append((tool_name, dict(args)))
        if tool_name == "write_file":
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""), encoding="utf-8")
            return f"Wrote {args['path']}"
        if tool_name == "edit_file":
            p = Path(args["path"])
            text = p.read_text(encoding="utf-8")
            for ed in args.get("edits", []):
                text = text.replace(ed["old_text"], ed["new_text"])
            p.write_text(text, encoding="utf-8")
            return f"Edited {args['path']}"
        if tool_name == "execute_shell":
            command = args.get("command", "")
            if command == "echo render-report":
                out = workspace / "out" / "report.txt"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("quarterly numbers\n", encoding="utf-8")
                return "rendered"
            if command == "echo append-data":
                target = workspace / "data.txt"
                with target.open("a", encoding="utf-8") as fh:
                    fh.write("row2\n")
                return "appended"
            if command == "echo read-files":
                # Pure reads: content + directory listing. Atime may
                # move; mtime/size must not — nothing may be collected.
                (workspace / "data.txt").read_text(encoding="utf-8")
                sorted(workspace.iterdir())
                return "read"
            if command == "echo remove-file":
                (workspace / "data.txt").unlink()
                return "removed"
            if command == "echo write-outside":
                (outside_dir / "escaped.txt").write_text(
                    "not a task deliverable\n", encoding="utf-8")
                return "wrote outside"
            return ""
        return ""

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _fake_worker)

    registry = ToolRegistry()
    registry.register(Tool("write_file", "write", {}, lambda **kw: "", risk=ToolRisk.WRITE))
    registry.register(Tool("edit_file", "edit", {}, lambda **kw: "", risk=ToolRisk.WRITE))
    registry.register(Tool("execute_shell", "shell", {}, lambda **kw: "", risk=ToolRisk.EXECUTE))

    def _sneaky_read(**kw):
        # A read-only tool whose implementation (perversely) writes:
        # collection is gated on the tool's risk class, so even this
        # must never be collected.
        (workspace / "sneaky.txt").write_text("should not surface\n", encoding="utf-8")
        return "read"

    registry.register(Tool("read_file", "read", {}, _sneaky_read, risk=ToolRisk.READ_ONLY))
    return registry, workspace, calls


def _state(task_id, session_id, name, args, call_id, **extra):
    state = {
        "task_id": task_id,
        "session_id": session_id,
        "pending_tool_call": {
            "name": name,
            "args": args,
            "id": call_id,
            "approved": True,
        },
    }
    state.update(extra)
    return state


def _config(registry, session_id, workspace):
    return {
        "configurable": {
            "tool_registry": registry,
            "session_id": session_id,
            "workspace_roots": [str(workspace)],
        }
    }


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    db_path = tmp_path / "artifacts.db"
    store = ArtifactStore(db_path=db_path)
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", store)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    registry, workspace, calls = _build_harness(tmp_path, monkeypatch, outside_dir)
    return store, registry, workspace, outside_dir, calls


@pytest.mark.asyncio
async def test_shell_created_file_is_collected(harness):
    from aether_engine.event_bus import event_bus
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    task_id, session_id = "task_n05_a", "session_n05_a"
    q: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe(task_id, q)
    try:
        state = _state(task_id, session_id, "execute_shell",
                       {"command": "echo render-report"}, "c1")
        await execute_tool_node(state, _config(registry, session_id, workspace))
    finally:
        events = []
        while not q.empty():
            events.append(await q.get())
        event_bus.unsubscribe(task_id, q)

    arts, total = store.list_artifacts(session_id=session_id)
    assert total == 1
    art = arts[0]
    created = workspace / "out" / "report.txt"
    assert art["kind"] == "file"
    assert art["title"] == "report.txt"
    assert art["file_path"] == str(created)
    assert art["task_id"] == task_id
    assert art["metadata"] == {"tool": "execute_shell"}

    recorded = [e for e in events if e.type == EventType.ARTIFACT_RECORDED]
    assert len(recorded) == 1
    assert recorded[0].payload["artifact_id"] == art["id"]
    assert recorded[0].payload["path"] == str(created)


@pytest.mark.asyncio
async def test_shell_modified_file_is_collected(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    (workspace / "data.txt").write_text("row1\n", encoding="utf-8")
    task_id, session_id = "task_n05_b", "session_n05_b"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo append-data"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    arts, total = store.list_artifacts(session_id=session_id)
    assert total == 1
    assert arts[0]["title"] == "data.txt"
    assert arts[0]["file_path"] == str(workspace / "data.txt")
    assert arts[0]["metadata"] == {"tool": "execute_shell"}


@pytest.mark.asyncio
async def test_shell_read_only_registers_nothing(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, calls = harness
    (workspace / "data.txt").write_text("row1\n", encoding="utf-8")
    task_id, session_id = "task_n05_c", "session_n05_c"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo read-files"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    assert ("execute_shell", {"command": "echo read-files"}) in calls  # it ran
    _arts, total = store.list_artifacts(session_id=session_id)
    assert total == 0


@pytest.mark.asyncio
async def test_shell_delete_registers_nothing(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    (workspace / "data.txt").write_text("row1\n", encoding="utf-8")
    task_id, session_id = "task_n05_d", "session_n05_d"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo remove-file"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    assert not (workspace / "data.txt").exists()  # the delete really happened
    _arts, total = store.list_artifacts(session_id=session_id)
    assert total == 0


@pytest.mark.asyncio
async def test_shell_write_outside_workspace_not_collected(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, outside_dir, _calls = harness
    task_id, session_id = "task_n05_e", "session_n05_e"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo write-outside"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    assert (outside_dir / "escaped.txt").is_file()  # the write happened
    _arts, total = store.list_artifacts(session_id=session_id)
    assert total == 0


@pytest.mark.asyncio
async def test_read_only_tool_never_collects(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    task_id, session_id = "task_n05_f", "session_n05_f"

    state = _state(task_id, session_id, "read_file",
                   {"path": str(workspace / "data.txt")}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    assert (workspace / "sneaky.txt").is_file()  # the tool really ran
    _arts, total = store.list_artifacts(session_id=session_id)
    assert total == 0


@pytest.mark.asyncio
async def test_plan_mode_collects_nothing(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    task_id, session_id = "task_n05_g", "session_n05_g"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo render-report"}, "c1", plan_mode=True)
    result = await execute_tool_node(state, _config(registry, session_id, workspace))

    assert "plan mode" in result["messages"][0]["content"]
    assert not (workspace / "out" / "report.txt").exists()
    _arts, total = store.list_artifacts(session_id=session_id)
    assert total == 0


@pytest.mark.asyncio
async def test_write_and_edit_paths_unchanged(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    store, registry, workspace, _outside, _calls = harness
    task_id, session_id = "task_n05_h", "session_n05_h"
    config = _config(registry, session_id, workspace)

    target = workspace / "target.txt"
    target.write_text("line1\nline2\n", encoding="utf-8")

    state_1 = _state(task_id, session_id, "write_file",
                     {"path": str(workspace / "script.py"),
                      "content": "print('hello')"}, "c1")
    await execute_tool_node(state_1, config)

    state_2 = _state(task_id, session_id, "edit_file",
                     {"path": str(target),
                      "edits": [{"old_text": "line1\n",
                                 "new_text": "line1_modified\n"}]}, "c2")
    await execute_tool_node(state_2, config)

    arts, total = store.list_artifacts(session_id=session_id)
    # Exactly the two established records — no duplicates from any
    # filesystem observation of the same writes.
    assert total == 2
    by_title = {a["title"]: a for a in arts}
    assert by_title["script.py"]["kind"] == "file"
    assert by_title["script.py"]["metadata"] == {"tool": "write_file"}
    diff_art = by_title["target.txt"]
    assert diff_art["kind"] == "diff"
    assert diff_art["content"] is not None
    assert "-line1" in diff_art["content"]
    assert "+line1_modified" in diff_art["content"]
