"""Catalogue sync + embedder lifecycle (models/catalogue-embedder).

Part A: downloaded models are typed from the daemon's /api/show
(capabilities -> chat / embedding / both / unknown), with family /
parameter_size / quantization carried into list_models(),
list_downloaded_models() and the hub's downloaded inventory; the
built-in catalogue lists the embedding models the engine itself
needs.

Part B: ensure_embedder's four states against a scripted daemon.
"""
from typing import ClassVar

import pytest

from aether_engine.models import local_models
from aether_engine.models.local_models import (
    DEFAULT_LOCAL_CATALOG,
    OllamaManager,
    ensure_embedder,
    model_type_from_capabilities,
)


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


def _show(family="qwen3", params="0.6B", quant="Q8_0", capabilities=None):
    return {
        "details": {
            "family": family,
            "parameter_size": params,
            "quantization_level": quant,
        },
        "capabilities": capabilities if capabilities is not None else ["embedding"],
    }


class _ScriptedClient:
    """Scripted Ollama daemon: tags + show + pull, all recorded."""

    tags: ClassVar[list] = []
    shows: ClassVar[dict] = {}
    show_status: ClassVar[dict] = {}
    pull_status = 200
    pull_text = ""
    running = True
    show_calls: ClassVar[list] = []
    pull_calls: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        if url.endswith("/api/tags"):
            return _Resp(200, {"models": list(type(self).tags)})
        return _Resp(200 if type(self).running else 404, {})

    async def post(self, url, json=None):
        if url.endswith("/api/show"):
            name = (json or {}).get("name", "")
            type(self).show_calls.append(name)
            status = type(self).show_status.get(name, 200)
            if status != 200:
                return _Resp(status, {})
            return _Resp(200, type(self).shows.get(name, {}))
        if url.endswith("/api/pull"):
            type(self).pull_calls.append((json or {}).get("name", ""))
            return _Resp(type(self).pull_status, {}, text=type(self).pull_text)
        return _Resp(404, {})


@pytest.fixture
def daemon(monkeypatch):
    _ScriptedClient.tags = []
    _ScriptedClient.shows = {}
    _ScriptedClient.show_status = {}
    _ScriptedClient.pull_status = 200
    _ScriptedClient.pull_text = ""
    _ScriptedClient.running = True
    _ScriptedClient.show_calls = []
    _ScriptedClient.pull_calls = []
    monkeypatch.setattr(local_models.httpx, "AsyncClient", _ScriptedClient)
    return _ScriptedClient


# --- capability typing ------------------------------------------------------

def test_model_type_from_capabilities():
    assert model_type_from_capabilities(["embedding"]) == "embedding"
    assert model_type_from_capabilities(["completion"]) == "chat"
    assert model_type_from_capabilities(["completion", "embedding"]) == "both"
    assert model_type_from_capabilities(["tools"]) == "unknown"
    assert model_type_from_capabilities([]) == "unknown"
    assert model_type_from_capabilities(None) == "unknown"
    assert model_type_from_capabilities("embedding") == "unknown"


def test_catalogue_lists_embedders_with_types():
    by_name = {item["name"]: item for item in DEFAULT_LOCAL_CATALOG}
    assert by_name["qwen3-embedding:0.6b"]["type"] == "embedding"
    assert by_name["nomic-embed-text"]["type"] == "embedding"
    # Every catalogue entry carries a type; the original four are chat.
    assert all(item.get("type") for item in DEFAULT_LOCAL_CATALOG)
    assert by_name["llama3:8b"]["type"] == "chat"


@pytest.mark.asyncio
async def test_list_models_types_downloaded_from_show(daemon):
    daemon.tags = [
        {"name": "qwen3-embedding:0.6b", "size": 639000000, "modified_at": "t1"},
        {"name": "llama3:8b", "size": 4700000000, "modified_at": "t2"},
        {"name": "mystery:latest", "size": 1000, "modified_at": "t3"},
    ]
    daemon.shows = {
        "qwen3-embedding:0.6b": _show(capabilities=["embedding"]),
        "llama3:8b": _show(family="llama", params="8.0B", quant="Q4_0",
                            capabilities=["completion", "tools"]),
    }
    daemon.show_status = {"mystery:latest": 500}

    mgr = OllamaManager()
    models = await mgr.list_models()
    by_name = {m["name"]: m for m in models if m["downloaded"]}

    emb = by_name["qwen3-embedding:0.6b"]
    assert emb["type"] == "embedding"
    assert emb["family"] == "qwen3"
    assert emb["parameter_size"] == "0.6B"
    assert emb["quantization"] == "Q8_0"

    chat = by_name["llama3:8b"]
    assert chat["type"] == "chat"
    assert chat["parameter_size"] == "8.0B"

    # A failed /api/show degrades to unknown — the model still lists.
    assert by_name["mystery:latest"]["type"] == "unknown"


@pytest.mark.asyncio
async def test_show_metadata_is_cached_per_name_and_stamp(daemon):
    daemon.tags = [
        {"name": "llama3:8b", "size": 4700000000, "modified_at": "t2"},
    ]
    daemon.shows = {"llama3:8b": _show(capabilities=["completion"])}
    mgr = OllamaManager()
    await mgr.list_models()
    await mgr.list_models()
    assert daemon.show_calls == ["llama3:8b"]


@pytest.mark.asyncio
async def test_list_downloaded_models_carries_type_metadata(daemon):
    daemon.tags = [
        {"name": "nomic-embed-text:latest", "size": 274000000,
         "modified_at": "t9"},
    ]
    daemon.shows = {
        "nomic-embed-text:latest": _show(family="nomic-bert",
                                         params="137M", quant="F16"),
    }
    mgr = OllamaManager()
    models = await mgr.list_downloaded_models()
    assert len(models) == 1
    entry = models[0]
    assert entry["size_bytes"] == 274000000
    assert entry["type"] == "embedding"
    assert entry["family"] == "nomic-bert"
    assert entry["parameter_size"] == "137M"
    assert entry["quantization"] == "F16"


@pytest.mark.asyncio
async def test_hub_inventory_labels_embedding_models(daemon, tmp_path):
    from aether_engine.models.hub_downloads import ModelHubDownloads

    daemon.tags = [
        {"name": "qwen3-embedding:0.6b", "size": 639000000,
         "modified_at": "t1"},
    ]
    daemon.shows = {"qwen3-embedding:0.6b": _show()}
    downloads = ModelHubDownloads(
        hub=None, ollama=OllamaManager(), hf_cache_dir=tmp_path / "hf")
    result = await downloads.downloaded()
    rows = [r for r in result["models"] if r["source"] == "ollama"]
    assert len(rows) == 1
    row = rows[0]
    assert row["family"] == "embedding"
    assert row["type"] == "embedding"
    assert row["params"] == "0.6B"
    assert row["quant"] == "Q8_0"


# --- ensure_embedder ---------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_embedder_ollama_off(daemon):
    # The scripted daemon answers the root probe with 404 when
    # "running" is False, which is what a dead daemon looks like to
    # is_ollama_running's 2-second probe.
    daemon.running = False
    status = await ensure_embedder(OllamaManager(), "qwen3-embedding:0.6b")
    assert status["state"] == "ollama_off"
    assert status["model"] == "qwen3-embedding:0.6b"
    assert daemon.pull_calls == []


@pytest.mark.asyncio
async def test_ensure_embedder_ready(daemon):
    daemon.tags = [{"name": "qwen3-embedding:0.6b", "size": 1,
                    "modified_at": "t1"}]
    status = await ensure_embedder(OllamaManager(), "qwen3-embedding:0.6b")
    assert status["state"] == "ready"
    assert daemon.pull_calls == []


@pytest.mark.asyncio
async def test_ensure_embedder_pulls_missing_model(daemon):
    daemon.tags = [{"name": "llama3:8b", "size": 1, "modified_at": "t2"}]
    status = await ensure_embedder(OllamaManager(), "qwen3-embedding:0.6b")
    assert status["state"] == "pulled"
    assert daemon.pull_calls == ["qwen3-embedding:0.6b"]


@pytest.mark.asyncio
async def test_ensure_embedder_pull_failed(daemon):
    daemon.tags = []
    daemon.pull_status = 500
    daemon.pull_text = "pull access denied"
    status = await ensure_embedder(OllamaManager(), "qwen3-embedding:0.6b")
    assert status["state"] == "pull_failed"
    assert "500" in status["detail"]
