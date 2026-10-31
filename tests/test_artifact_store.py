import sqlite3
import time
from pathlib import Path

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts.store import ArtifactStore


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_artifacts.db"
    return db_file


@pytest.fixture
def store(temp_db):
    return ArtifactStore(db_path=temp_db)


# ==============================================================================
# Store CRUD + PRAGMA user_version tests
# ==============================================================================

def test_store_initialization_and_schema(temp_db):
    store = ArtifactStore(db_path=temp_db)
    store.init_db()

    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()

    # Check user_version PRAGMA = 1
    cur.execute("PRAGMA user_version")
    ver = cur.fetchone()[0]
    assert ver == 1

    # Check artifacts table exists and columns match
    cur.execute("PRAGMA table_info(artifacts)")
    columns = {row[1]: row[2] for row in cur.fetchall()}
    assert "id" in columns
    assert "session_id" in columns
    assert "task_id" in columns
    assert "kind" in columns
    assert "title" in columns
    assert "file_path" in columns
    assert "content" in columns
    assert "metadata_json" in columns
    assert "created_at" in columns
    assert "updated_at" in columns
    conn.close()


def test_record_and_get_artifact(store, tmp_path):
    py_file = tmp_path / "hello.py"
    py_file.write_text("print('hello')", encoding="utf-8")

    art = store.record_artifact(
        session_id="sess_1",
        task_id="task_1",
        kind="file",
        title="hello.py",
        file_path=str(py_file),
        content=None,
        metadata={"tool": "write_file"},
    )

    assert art["id"] is not None
    assert art["session_id"] == "sess_1"
    assert art["task_id"] == "task_1"
    assert art["kind"] == "file"
    assert art["title"] == "hello.py"
    assert art["file_path"] == str(py_file)
    assert art["metadata"] == {"tool": "write_file"}

    # Fetch it back
    fetched = store.get_artifact(art["id"])
    assert fetched is not None
    assert fetched["id"] == art["id"]
    assert fetched["title"] == "hello.py"
    assert fetched["file_path"] == str(py_file)


def test_list_artifacts_grouping_and_filtering(store):
    # Session 1, Task 1: 2 artifacts
    store.record_artifact("sess_1", "task_1", "file", "main.py", "/app/main.py")
    time.sleep(0.01)
    store.record_artifact("sess_1", "task_1", "file", "README.md", "/app/README.md")

    # Session 1, Task 2: 1 artifact
    time.sleep(0.01)
    store.record_artifact("sess_1", "task_2", "diff", "main.py", "/app/main.py", content="--- main.py\n+++ main.py\n@@ -1 +1 @@\n-old\n+new")

    # Session 2, Task 3: 1 artifact
    store.record_artifact("sess_2", "task_3", "file", "other.py", "/app/other.py")

    # List for Session 1
    items_s1, total_s1 = store.list_artifacts(session_id="sess_1")
    assert total_s1 == 3
    assert len(items_s1) == 3
    # Check newest first
    assert items_s1[0]["task_id"] == "task_2"
    assert items_s1[0]["kind"] == "diff"

    # List for Session 2
    items_s2, total_s2 = store.list_artifacts(session_id="sess_2")
    assert total_s2 == 1
    assert items_s2[0]["task_id"] == "task_3"

    # Global list
    _items_all, total_all = store.list_artifacts(is_global=True)
    assert total_all == 4


def test_session_cascade_delete(store):
    store.record_artifact("sess_1", "task_1", "file", "a.py", "/a.py")
    store.record_artifact("sess_1", "task_2", "file", "b.py", "/b.py")
    store.record_artifact("sess_2", "task_3", "file", "c.py", "/c.py")

    deleted_count = store.delete_by_session("sess_1")
    assert deleted_count == 2

    # Session 1 artifacts should be gone
    items_s1, total_s1 = store.list_artifacts(session_id="sess_1")
    assert total_s1 == 0
    assert len(items_s1) == 0

    # Session 2 artifacts remain intact
    _items_s2, total_s2 = store.list_artifacts(session_id="sess_2")
    assert total_s2 == 1


# ==============================================================================
# Auto-collection from execute_tool_node tests
# ==============================================================================

@pytest.mark.asyncio
async def test_execute_tool_node_auto_collection(tmp_path, monkeypatch):
    from aether_engine.event_bus import event_bus
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
    from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk
    from aether_engine.workers import sandbox

    db_path = tmp_path / "artifacts.db"
    test_store = ArtifactStore(db_path=db_path)
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    registry = ToolRegistry()
    test_py = tmp_path / "script.py"
    test_md = tmp_path / "doc.md"
    test_target = tmp_path / "target.txt"
    test_target.write_text("line1\nline2\n", encoding="utf-8")

    async def _in_process_worker(tool_name, args, task_class="standard", audit_logger=None):
        if tool_name == "write_file":
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content", ""), encoding="utf-8")
            return f"Wrote {args['path']}"
        elif tool_name == "edit_file":
            p = Path(args["path"])
            text = p.read_text(encoding="utf-8")
            for ed in args.get("edits", []):
                text = text.replace(ed["old_text"], ed["new_text"])
            p.write_text(text, encoding="utf-8")
            return f"Edited {args['path']}"
        return ""

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _in_process_worker)

    registry.register(Tool("write_file", "write", {}, lambda **kw: "", risk=ToolRisk.WRITE))
    registry.register(Tool("edit_file", "edit", {}, lambda **kw: "", risk=ToolRisk.WRITE))

    events_captured = []
    task_id = "task_slice_a"
    session_id = "session_slice_a"

    import asyncio
    q = asyncio.Queue()
    event_bus.subscribe(task_id, q)

    # 1. Write script.py
    state_1 = {
        "task_id": task_id,
        "session_id": session_id,
        "pending_tool_call": {
            "name": "write_file",
            "args": {"path": str(test_py), "content": "print('hello')"},
            "id": "c1",
            "approved": True,
        },
    }
    config = {
        "configurable": {
            "tool_registry": registry,
            "session_id": session_id,
            "workspace_roots": [str(tmp_path)],
        }
    }
    res_1 = await execute_tool_node(state_1, config)
    assert res_1["write_tool_used"] is True

    # 2. Write doc.md
    state_2 = {
        "task_id": task_id,
        "session_id": session_id,
        "pending_tool_call": {
            "name": "write_file",
            "args": {"path": str(test_md), "content": "# Readme"},
            "id": "c2",
            "approved": True,
        },
    }
    await execute_tool_node(state_2, config)

    # 3. Edit target.txt
    state_3 = {
        "task_id": task_id,
        "session_id": session_id,
        "pending_tool_call": {
            "name": "edit_file",
            "args": {
                "path": str(test_target),
                "edits": [{"old_text": "line1\n", "new_text": "line1_modified\n"}],
            },
            "id": "c3",
            "approved": True,
        },
    }
    await execute_tool_node(state_3, config)

    # Verify 3 artifacts stored
    arts, total = test_store.list_artifacts(session_id=session_id)
    assert total == 3
    titles = [a["title"] for a in arts]
    assert "script.py" in titles
    assert "doc.md" in titles
    assert "target.txt" in titles

    # Check the diff artifact
    diff_art = next(a for a in arts if a["title"] == "target.txt")
    assert diff_art["kind"] == "diff"
    assert diff_art["content"] is not None
    assert "-line1" in diff_art["content"]
    assert "+line1_modified" in diff_art["content"]

    # Check event_bus captured ARTIFACT_RECORDED events
    while not q.empty():
        events_captured.append(await q.get())

    recorded_evs = [e for e in events_captured if e.type == EventType.ARTIFACT_RECORDED]
    assert len(recorded_evs) == 3
    event_bus.unsubscribe(task_id, q)


# ==============================================================================
# WebSocket handlers & Missing file placeholder tests
# ==============================================================================

@pytest.mark.asyncio
async def test_websocket_artifact_get_and_missing_file(tmp_path, monkeypatch):
    from aether_engine.routes.websocket import handle_artifact_get

    db_path = tmp_path / "artifacts.db"
    test_store = ArtifactStore(db_path=db_path)
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    real_file = tmp_path / "existing.py"
    real_file.write_text("print('exists')", encoding="utf-8")

    art1 = test_store.record_artifact("sess_1", "task_1", "file", "existing.py", str(real_file))
    
    # 1. Existing file get
    resp1 = await handle_artifact_get(art1["id"])
    assert resp1["exists"] is True
    assert resp1["content"] == "print('exists')"
    assert resp1["error"] is None

    # 2. Deleted file on disk -> degrades cleanly with exists=False
    real_file.unlink()
    resp2 = await handle_artifact_get(art1["id"])
    assert resp2["exists"] is False
    assert resp2["content"] is None
    assert "no longer exists" in resp2["error"]
    assert resp2["artifact"]["id"] == art1["id"]
