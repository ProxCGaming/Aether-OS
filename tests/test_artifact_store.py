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
# Epic 8, E8-2.3: Global Cross-Session Listing, Kind Filtering & Search
# ==============================================================================

def test_cross_session_listing_and_filtering_by_kind(store):
    store.record_artifact("sess_a", "task_1", "file", "alpha.py", "/alpha.py")
    store.record_artifact("sess_a", "task_2", "diff", "alpha.py", "/alpha.py", content="diff")
    store.record_artifact("sess_b", "task_3", "file", "beta.md", "/beta.md")
    store.record_artifact("sess_b", "task_4", "report", "summary.md", "/summary.md")
    store.record_artifact("sess_c", "task_5", "file", "gamma.json", "/gamma.json")

    # 1. Global list returns artifacts from all sessions with proper attribution
    all_arts, total_all = store.list_artifacts(is_global=True)
    assert total_all == 5
    assert len(all_arts) == 5
    sessions_found = {a["session_id"] for a in all_arts}
    assert sessions_found == {"sess_a", "sess_b", "sess_c"}
    tasks_found = {a["task_id"] for a in all_arts}
    assert tasks_found == {"task_1", "task_2", "task_3", "task_4", "task_5"}

    # 2. Filter by kind across sessions
    file_arts, file_total = store.list_artifacts(is_global=True, kind="file")
    assert file_total == 3
    assert all(a["kind"] == "file" for a in file_arts)
    assert {a["title"] for a in file_arts} == {"alpha.py", "beta.md", "gamma.json"}

    diff_arts, diff_total = store.list_artifacts(is_global=True, kind="diff")
    assert diff_total == 1
    assert diff_arts[0]["kind"] == "diff"
    assert diff_arts[0]["session_id"] == "sess_a"

    report_arts, report_total = store.list_artifacts(is_global=True, kind="report")
    assert report_total == 1
    assert report_arts[0]["title"] == "summary.md"

    # 3. Filter by session_id and kind combined
    sess_a_files, sess_a_total = store.list_artifacts(session_id="sess_a", kind="file")
    assert sess_a_total == 1
    assert sess_a_files[0]["title"] == "alpha.py"


def test_search_artifacts_in_store(store):
    store.record_artifact("sess_login", "task_auth", "file", "auth_controller.py", "/src/auth_controller.py")
    store.record_artifact("sess_payment", "task_stripe", "diff", "checkout.js", "/src/checkout.js")
    store.record_artifact("sess_reports", "task_metrics", "file", "analytics.json", "/data/analytics.json")

    # Search by title
    res_title, total_title = store.list_artifacts(is_global=True, search="auth_controller")
    assert total_title == 1
    assert res_title[0]["title"] == "auth_controller.py"

    # Search by session_id
    res_sess, total_sess = store.list_artifacts(is_global=True, search="sess_payment")
    assert total_sess == 1
    assert res_sess[0]["session_id"] == "sess_payment"

    # Search by task_id
    res_task, total_task = store.list_artifacts(is_global=True, search="task_metrics")
    assert total_task == 1
    assert res_task[0]["task_id"] == "task_metrics"

    # Search with no match
    res_none, total_none = store.list_artifacts(is_global=True, search="nonexistent")
    assert total_none == 0
    assert len(res_none) == 0


def test_session_delete_cascades_only_target_session_globally(store):
    store.record_artifact("session_one", "task_1", "file", "file1.txt", "/file1.txt")
    store.record_artifact("session_one", "task_2", "diff", "file1.txt", "/file1.txt")
    store.record_artifact("session_two", "task_3", "file", "file2.txt", "/file2.txt")
    store.record_artifact("session_three", "task_4", "file", "file3.txt", "/file3.txt")

    # Before delete: 4 total across 3 sessions
    _, before_total = store.list_artifacts(is_global=True)
    assert before_total == 4

    # Delete session_one
    deleted = store.delete_by_session("session_one")
    assert deleted == 2

    # After delete: session_one is gone, session_two and session_three are completely untouched
    after_arts, after_total = store.list_artifacts(is_global=True)
    assert after_total == 2
    assert len(after_arts) == 2
    remaining_sessions = {a["session_id"] for a in after_arts}
    assert remaining_sessions == {"session_two", "session_three"}
    assert all(a["session_id"] != "session_one" for a in after_arts)


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

    async def _in_process_worker(tool_name, args, task_class="standard", audit_logger=None, task_classes=None):
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


# ==============================================================================
# S4-M9 residual: artifact collection must upsert per (session, task, file)
# ==============================================================================

def test_s4m9_same_file_repeated_record_upserts_single_row(store):
    """(a) Same (session, task, file_path) twice -> ONE row, latest content."""
    first = store.record_artifact(
        "sess_s4m9", "task_s4m9", "file", "main.py", "/app/main.py",
        content="v1",
    )
    time.sleep(0.01)
    second = store.record_artifact(
        "sess_s4m9", "task_s4m9", "file", "main.py", "/app/main.py",
        content="v2",
    )

    assert second["id"] == first["id"]
    items, total = store.list_artifacts(session_id="sess_s4m9")
    assert total == 1
    assert len(items) == 1
    assert items[0]["id"] == first["id"]
    assert items[0]["content"] == "v2"
    assert items[0]["created_at"] == first["created_at"]
    assert items[0]["updated_at"] >= first["created_at"]
    # updated_at should reflect the second record, not the first insert
    assert items[0]["updated_at"] >= second["updated_at"] or items[0]["updated_at"] >= first["updated_at"]


def test_s4m9_same_path_different_task_mints_separate_rows(store):
    """(b) Same path but different task_id -> two rows."""
    store.record_artifact(
        "sess_s4m9", "task_a", "file", "main.py", "/app/main.py", content="v1",
    )
    store.record_artifact(
        "sess_s4m9", "task_b", "file", "main.py", "/app/main.py", content="v2",
    )

    items, total = store.list_artifacts(session_id="sess_s4m9")
    assert total == 2
    assert {a["task_id"] for a in items} == {"task_a", "task_b"}


def test_s4m9_kind_updates_on_upsert_file_to_diff(store):
    """(c) Same triple, kind changes file->diff -> one row, kind == 'diff'."""
    store.record_artifact(
        "sess_s4m9", "task_s4m9", "file", "main.py", "/app/main.py", content="v1",
    )
    time.sleep(0.01)
    store.record_artifact(
        "sess_s4m9", "task_s4m9", "diff", "main.py", "/app/main.py",
        content="--- a\n+++ b\n",
    )

    items, total = store.list_artifacts(session_id="sess_s4m9")
    assert total == 1
    assert items[0]["kind"] == "diff"
    assert items[0]["content"] == "--- a\n+++ b\n"


def test_s4m9_none_file_path_does_not_merge(store):
    """(d) file_path=None recorded twice -> two rows (no accidental merge)."""
    store.record_artifact(
        session_id="sess_s4m9",
        task_id="task_s4m9",
        kind="file",
        title="note.txt",
        file_path=None,
        content="one",
    )
    store.record_artifact(
        session_id="sess_s4m9",
        task_id="task_s4m9",
        kind="file",
        title="note.txt",
        file_path=None,
        content="two",
    )

    _items, total = store.list_artifacts(session_id="sess_s4m9")
    assert total == 2


def test_s4m9_explicit_artifact_id_still_upserts_by_id(store):
    """(e) Explicit artifact_id path still upserts by id."""
    first = store.record_artifact(
        session_id="sess_s4m9",
        task_id="task_s4m9",
        kind="file",
        title="explicit.py",
        file_path="/app/explicit.py",
        content="v1",
        artifact_id="art-explicit-s4m9",
    )
    assert first["id"] == "art-explicit-s4m9"
    time.sleep(0.01)
    second = store.record_artifact(
        session_id="sess_s4m9",
        task_id="task_s4m9",
        kind="diff",
        title="explicit.py",
        file_path="/app/explicit.py",
        content="v2",
        artifact_id="art-explicit-s4m9",
    )
    assert second["id"] == "art-explicit-s4m9"

    items, total = store.list_artifacts(session_id="sess_s4m9")
    assert total == 1
    assert items[0]["content"] == "v2"
    assert items[0]["kind"] == "diff"



# ==============================================================================
# S4-N1 — artifact GET content cap
# ==============================================================================

@pytest.mark.asyncio
async def test_artifact_get_caps_oversized_text(tmp_path, monkeypatch):
    from aether_engine.routes.websocket import (
        ARTIFACT_CONTENT_CAP_BYTES,
        handle_artifact_get,
    )

    test_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    big = tmp_path / "big.log"
    big.write_bytes(b"a" * (ARTIFACT_CONTENT_CAP_BYTES + 100))
    art = test_store.record_artifact("sess_1", "task_1", "file", "big.log", str(big))

    resp = await handle_artifact_get(art["id"])
    assert resp["exists"] is True
    assert resp["error"] is None
    assert resp["content"].startswith("a" * 1000)
    assert resp["content"].endswith("artifact preview cap]")
    assert len(resp["content"]) < ARTIFACT_CONTENT_CAP_BYTES + 200


@pytest.mark.asyncio
async def test_artifact_get_refuses_oversized_image(tmp_path, monkeypatch):
    from aether_engine.routes.websocket import (
        ARTIFACT_CONTENT_CAP_BYTES,
        handle_artifact_get,
    )

    test_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    big_img = tmp_path / "big.png"
    big_img.write_bytes(b"\x89PNG" + b"\x00" * ARTIFACT_CONTENT_CAP_BYTES)
    art = test_store.record_artifact("sess_1", "task_1", "file", "big.png", str(big_img))

    resp = await handle_artifact_get(art["id"])
    assert resp["content"] is None
    assert "too large to preview" in resp["error"]


@pytest.mark.asyncio
async def test_artifact_get_small_image_still_returns_data_url(tmp_path, monkeypatch):
    from aether_engine.routes.websocket import handle_artifact_get

    test_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr("aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", test_store)

    small = tmp_path / "small.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n")
    art = test_store.record_artifact("sess_1", "task_1", "file", "small.png", str(small))

    resp = await handle_artifact_get(art["id"])
    assert resp["error"] is None
    assert resp["content"].startswith("data:image/png;base64,")
