"""PF-2 — shell-created media must enter the media library.

N05's filesystem-effect observation collects files a shell command
creates as canvas artifacts, but media registration (+ analysis
scheduling) only ran on the write_file/edit_file path: a shell-rendered
image or video reached the canvas yet never entered the media library,
so it was never analysed and library search / media recall could never
find it. The board acceptance: a shell command that renders an image
produces BOTH the canvas artifact and a generated-media library entry
with analysis scheduled; non-media outputs register no media entry.

Harness mirrors tests/test_epic8_n05_artifact_collection.py: a fake
in-process worker stands in for the sandbox subprocess and writes real
bytes to the real filesystem, so the observation seam (snapshot diff
in execute_tool_node) is exercised exactly as in production.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts.store import ArtifactStore
from aether_engine.media.store import MediaStore

# A real 1x1 transparent PNG and a plausible MP4 ftyp header — the
# shell writes actual media bytes, the way a render command would.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000000020001e221bc330000"
    "000049454e44ae426082"
)
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32


def _build_harness(tmp_path, monkeypatch, outside_dir: Path):
    """Registry + fake worker whose shell commands have scripted effects."""
    from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk
    from aether_engine.workers import sandbox

    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def _fake_worker(tool_name, args, task_class="standard", audit_logger=None, task_classes=None):
        if tool_name == "write_file":
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(PNG_BYTES)
            return f"Wrote {args['path']}"
        if tool_name == "execute_shell":
            command = args.get("command", "")
            if command == "echo render-image":
                out = workspace / "out" / "render.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(PNG_BYTES)
                return "rendered"
            if command == "echo render-video":
                (workspace / "clip.mp4").write_bytes(MP4_BYTES)
                return "rendered"
            if command == "echo render-report":
                (workspace / "report.txt").write_text(
                    "quarterly numbers\n", encoding="utf-8")
                return "rendered"
            if command == "echo append-photo":
                with (workspace / "photo.png").open("ab") as fh:
                    fh.write(b"\x00")
                return "appended"
            if command == "echo render-outside-image":
                (outside_dir / "escaped.png").write_bytes(PNG_BYTES)
                return "wrote outside"
            return ""
        return ""

    monkeypatch.setattr(sandbox, "execute_tool_in_worker", _fake_worker)

    registry = ToolRegistry()
    registry.register(Tool("write_file", "write", {}, lambda **kw: "", risk=ToolRisk.WRITE))
    registry.register(Tool("execute_shell", "shell", {}, lambda **kw: "", risk=ToolRisk.EXECUTE))
    return registry, workspace


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
    art_store = ArtifactStore(db_path=tmp_path / "artifacts.db")
    monkeypatch.setattr(
        "aether_engine.artifacts.store.GLOBAL_ARTIFACT_STORE", art_store)
    media_store = MediaStore(db_path=tmp_path / "media.db")
    media_store.init_db()
    monkeypatch.setattr(
        "aether_engine.media.store.GLOBAL_MEDIA_STORE", media_store)
    scheduled: list[str] = []
    monkeypatch.setattr(
        "aether_engine.media.analyzer.schedule_media_analysis",
        lambda media_id: scheduled.append(media_id))
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    registry, workspace = _build_harness(tmp_path, monkeypatch, outside_dir)
    return art_store, media_store, registry, workspace, outside_dir, scheduled


@pytest.mark.asyncio
async def test_shell_rendered_image_registers_generated_media(harness):
    from aether_engine.event_bus import event_bus
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    art_store, media_store, registry, workspace, _outside, scheduled = harness
    task_id, session_id = "task_pf2_a", "session_pf2_a"
    q: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe(task_id, q)
    try:
        state = _state(task_id, session_id, "execute_shell",
                       {"command": "echo render-image"}, "c1",
                       message_id="user-msg-7")
        await execute_tool_node(state, _config(registry, session_id, workspace))
    finally:
        events = []
        while not q.empty():
            events.append(await q.get())
        event_bus.unsubscribe(task_id, q)

    created = workspace / "out" / "render.png"

    # The canvas artifact still lands (N05 behaviour preserved).
    _arts, art_total = art_store.list_artifacts(session_id=session_id)
    assert art_total == 1

    # …and now the media library entry too, in the generated collection.
    items, total = media_store.list_media(collection="generated")
    assert total == 1
    item = items[0]
    assert item["file_path"] == str(created)
    assert item["media_type"] == "image"
    assert item["session_id"] == session_id
    # N08 provenance parity with the write path.
    assert item["message_id"] == "user-msg-7"
    assert item["probe_metadata"]["origin"] == "agent_generated"
    assert item["probe_metadata"]["tool"] == "execute_shell"

    # Analysis was scheduled for exactly this entry.
    assert scheduled == [item["id"]]

    # The UI event fired, carrying the registered item.
    recorded = [e for e in events if e.type == EventType.MEDIA_RECORDED]
    assert len(recorded) == 1
    assert recorded[0].payload["id"] == item["id"]

    # Library search / recall can find it by its file name.
    hits = media_store.search_media("render")
    assert [h["id"] for h in hits] == [item["id"]]


@pytest.mark.asyncio
async def test_shell_rendered_video_registers_generated_media(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    _art, media_store, registry, workspace, _outside, scheduled = harness
    task_id, session_id = "task_pf2_b", "session_pf2_b"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo render-video"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    items, total = media_store.list_media(collection="generated")
    assert total == 1
    assert items[0]["file_path"] == str(workspace / "clip.mp4")
    assert items[0]["media_type"] == "video"
    assert scheduled == [items[0]["id"]]


@pytest.mark.asyncio
async def test_shell_non_media_output_registers_no_media(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    art_store, media_store, registry, workspace, _outside, scheduled = harness
    task_id, session_id = "task_pf2_c", "session_pf2_c"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo render-report"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    _arts, art_total = art_store.list_artifacts(session_id=session_id)
    assert art_total == 1  # the artifact still lands
    _items, media_total = media_store.list_media()
    assert media_total == 0
    assert scheduled == []


@pytest.mark.asyncio
async def test_write_file_media_control_registers(harness):
    """Control: the pre-existing registration path works in this
    harness, so the shell cases above isolate the real gap."""
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    _art, media_store, registry, workspace, _outside, scheduled = harness
    task_id, session_id = "task_pf2_d", "session_pf2_d"

    state = _state(task_id, session_id, "write_file",
                   {"path": str(workspace / "drawn.png"), "content": "x"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    items, total = media_store.list_media(collection="generated")
    assert total == 1
    assert items[0]["media_type"] == "image"
    assert scheduled == [items[0]["id"]]


@pytest.mark.asyncio
async def test_shell_touch_of_registered_media_does_not_duplicate(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    _art, media_store, registry, workspace, _outside, scheduled = harness
    task_id, session_id = "task_pf2_e", "session_pf2_e"
    config = _config(registry, session_id, workspace)

    # First the file-tool path registers the photo…
    state_1 = _state(task_id, session_id, "write_file",
                     {"path": str(workspace / "photo.png"), "content": "x"}, "c1")
    await execute_tool_node(state_1, config)
    items, total = media_store.list_media()
    assert total == 1
    original_id = items[0]["id"]

    # …then a shell command modifies the same file. It is already in
    # the library: no second entry, no re-scheduling.
    state_2 = _state(task_id, session_id, "execute_shell",
                     {"command": "echo append-photo"}, "c2")
    await execute_tool_node(state_2, config)

    items, total = media_store.list_media()
    assert total == 1
    assert items[0]["id"] == original_id
    assert scheduled == [original_id]


@pytest.mark.asyncio
async def test_shell_media_outside_workspace_not_registered(harness):
    from aether_engine.langgraph.nodes.execute_tool import execute_tool_node

    _art, media_store, registry, workspace, outside_dir, scheduled = harness
    task_id, session_id = "task_pf2_f", "session_pf2_f"

    state = _state(task_id, session_id, "execute_shell",
                   {"command": "echo render-outside-image"}, "c1")
    await execute_tool_node(state, _config(registry, session_id, workspace))

    assert (outside_dir / "escaped.png").is_file()  # the write happened
    _items, media_total = media_store.list_media()
    assert media_total == 0
    assert scheduled == []
