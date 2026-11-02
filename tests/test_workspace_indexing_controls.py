"""Tests for E8-3.3: Workspace Indexing controls (contracts, pause/resume, config, and WS handlers)."""

from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.rag.prose_indexer import ProseIndexer
from aether_engine.rag.store import WorkspaceRagStore


def _write_file(root: Path, rel: str, content: str = "content\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_contracts_event_types_exist():
    assert hasattr(EventType, "WORKSPACE_INDEX_PAUSE_REQUEST")
    assert hasattr(EventType, "WORKSPACE_INDEX_PAUSE_RESPONSE")
    assert hasattr(EventType, "WORKSPACE_INDEX_CONFIG_REQUEST")
    assert hasattr(EventType, "WORKSPACE_INDEX_CONFIG_RESPONSE")
    assert EventType.WORKSPACE_INDEX_PAUSE_REQUEST == "WORKSPACE_INDEX_PAUSE_REQUEST"
    assert EventType.WORKSPACE_INDEX_PAUSE_RESPONSE == "WORKSPACE_INDEX_PAUSE_RESPONSE"
    assert EventType.WORKSPACE_INDEX_CONFIG_REQUEST == "WORKSPACE_INDEX_CONFIG_REQUEST"
    assert EventType.WORKSPACE_INDEX_CONFIG_RESPONSE == "WORKSPACE_INDEX_CONFIG_RESPONSE"


async def test_indexer_pause_and_resume(tmp_path):
    rag_db = tmp_path / "rag.db"
    store = WorkspaceRagStore(rag_db)
    indexer = ProseIndexer(store=store)

    assert not indexer.paused
    st = indexer.status()
    assert st["status"] == "idle"
    assert st["paused"] is False

    indexer.pause()
    assert indexer.paused is True
    st_paused = indexer.status()
    assert st_paused["status"] == "paused"
    assert st_paused["paused"] is True

    indexer.resume()
    assert indexer.paused is False
    st_resumed = indexer.status()
    assert st_resumed["status"] == "idle"
    assert st_resumed["paused"] is False


async def test_indexer_status_surfaces_indexed_docs_and_excluded_reasons(tmp_path):
    rag_db = tmp_path / "rag.db"
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_file(ws, "readme.md", "# Guide\n")
    _write_file(ws, "notes.txt", "Some notes\n")
    _write_file(ws, ".gitignore", "secret.md\n")
    _write_file(ws, "secret.md", "classified\n")
    _write_file(ws, "large.md", "x" * (600 * 1024))  # >512KB

    store = WorkspaceRagStore(rag_db)
    indexer = ProseIndexer(store=store)
    indexer.grant_consent(str(ws))

    async def _fake_embed(text, *args, **kwargs):
        return [0.1] * 1024

    with patch("aether_engine.rag.prose_indexer.episodic.embed_text", side_effect=_fake_embed):
        await indexer.index_workspace(str(ws))

    st = indexer.status(str(ws))
    assert st["indexed_files"] == 2
    assert "indexed_docs" in st
    indexed_paths = {d["file_path"] for d in st["indexed_docs"]}
    assert indexed_paths == {"readme.md", "notes.txt"}

    excluded_map = {e["file_path"]: e["reason"] for e in st["excluded_files"]}
    assert excluded_map["secret.md"] == "gitignore"
    assert excluded_map["large.md"] == "too_large"


async def test_websocket_pause_and_config_handlers(tmp_path, monkeypatch):
    token_file = tmp_path / "engine.token"
    audit_file = tmp_path / "audit.log"
    rag_db = tmp_path / "rag_ws.db"

    # Scope every engine-state mutation to this test (N12): the raw
    # assignments used to leak into later suites — worst of all,
    # initialize_auth() starts the shared capability scheduler bound
    # to THIS test's event loop, and pytest-asyncio closes that loop
    # at teardown, so a later suite's app shutdown died stopping a
    # scheduler whose loop was already closed. monkeypatch restores
    # the attributes; the finally below stops the scheduler while
    # this test's loop is still alive (the lifespan-shutdown step a
    # bare TestClient(app) never runs for this test).
    monkeypatch.setattr(engine_state, "token_path", token_file)
    monkeypatch.setattr(engine_state.audit_logger, "log_path", audit_file)
    monkeypatch.setattr(
        engine_state, "user_config",
        UserConfig(embedding_model="qwen3-embedding:0.6b"))
    monkeypatch.setattr(engine_state, "auth_token", engine_state.auth_token)
    token = engine_state.initialize_auth()

    custom_indexer = ProseIndexer(store=WorkspaceRagStore(rag_db), config=engine_state.user_config)

    try:
        with patch("aether_engine.rag.prose_indexer.get_prose_indexer", return_value=custom_indexer):
            client = TestClient(app)
            with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
                # Drain HELLO on connect
                hello = Event.from_json(ws.receive_text())
                assert hello.type == EventType.HELLO

                # 1. Test Pause
                pause_req = Event(
                    type=EventType.WORKSPACE_INDEX_PAUSE_REQUEST,
                    request_id="pause-1",
                    payload={"workspace_root": str(tmp_path), "pause": True},
                )
                ws.send_text(pause_req.to_json())
                resp = Event.from_json(ws.receive_text())
                assert resp.type == EventType.WORKSPACE_INDEX_PAUSE_RESPONSE
                assert resp.request_id == "pause-1"
                assert resp.payload["paused"] is True
                assert custom_indexer.paused is True

                # 2. Test Resume
                resume_req = Event(
                    type=EventType.WORKSPACE_INDEX_PAUSE_REQUEST,
                    request_id="pause-2",
                    payload={"workspace_root": str(tmp_path), "pause": False},
                )
                ws.send_text(resume_req.to_json())
                resp = Event.from_json(ws.receive_text())
                assert resp.type == EventType.WORKSPACE_INDEX_PAUSE_RESPONSE
                assert resp.payload["paused"] is False
                assert custom_indexer.paused is False

                # 3. Test Config change to new embedder
                cfg_req = Event(
                    type=EventType.WORKSPACE_INDEX_CONFIG_REQUEST,
                    request_id="cfg-1",
                    payload={"workspace_root": str(tmp_path), "embedder": "qwen3-embedding:4b"},
                )
                ws.send_text(cfg_req.to_json())
                resp = Event.from_json(ws.receive_text())
                assert resp.type == EventType.WORKSPACE_INDEX_CONFIG_RESPONSE
                assert resp.request_id == "cfg-1"
                assert resp.payload["ok"] is True
                assert resp.payload["reindex_required"] is True
                assert custom_indexer.embedder == "qwen3-embedding:4b"
                assert engine_state.user_config.embedding_model == "qwen3-embedding:4b"

                # 4. Test Config with same embedder -> reindex_required is False
                cfg_same = Event(
                    type=EventType.WORKSPACE_INDEX_CONFIG_REQUEST,
                    request_id="cfg-2",
                    payload={"workspace_root": str(tmp_path), "embedder": "qwen3-embedding:4b"},
                )
                ws.send_text(cfg_same.to_json())
                resp = Event.from_json(ws.receive_text())
                assert resp.type == EventType.WORKSPACE_INDEX_CONFIG_RESPONSE
                assert resp.payload["reindex_required"] is False
    finally:
        engine_state.shutdown()
