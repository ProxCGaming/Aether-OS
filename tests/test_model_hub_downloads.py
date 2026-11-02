"""Model hub downloads (H2) — download managers + downloaded inventory.

Builds on H1's catalogue layer (``model_hub.py``): a hub download
routes by the H1 RUNNABILITY label —

- ``"ollama"`` rows and HF GGUF variants pull through the EXISTING
  Ollama machinery (``OllamaManager.download_model_task``); an HF
  GGUF variant becomes an Ollama pull of ``hf.co/<repo>:<quant>``.
- ``"in-app"`` speech repos (faster-whisper CT2, Kokoro) download
  into the standard Hugging Face cache via ``huggingface_hub`` so
  the voice code finds them on first load. When the library is not
  importable (it ships with the voice extra), the refusal is
  honest: "voice components are not installed".
- ``"not-runnable"`` / ``"unknown"`` rows refuse with the H1 reason.

One download at a time PER STORE (ollama / hf); a concurrent
request gets an honest busy refusal, never a silent queue.

Everything network-shaped is faked: no real Ollama daemon, no real
HF traffic, no multi-GB downloads.
"""

import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.models import local_models
from aether_engine.models.hub_downloads import (
    ModelHubDownloads,
    hf_gguf_ollama_ref,
)
from aether_engine.models.model_hub import normalise_hf_detail

# ---------------------------------------------------------------------------
# Fixture HF detail payloads (normalised through the real H1 code)
# ---------------------------------------------------------------------------

GGUF_DETAIL = normalise_hf_detail({
    "id": "bartowski/Llama-3.2-3B-Instruct-GGUF",
    "pipeline_tag": "text-generation",
    "library_name": "gguf",
    "downloads": 1000,
    "tags": ["gguf", "license:llama3.2"],
    "cardData": {"license": "llama3.2"},
    "siblings": [
        {"rfilename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf", "size": 2000},
        {"rfilename": "Llama-3.2-3B-Instruct-Q8_0.gguf", "size": 3400},
        {"rfilename": "README.md", "size": 100},
    ],
})

SINGLE_GGUF_DETAIL = normalise_hf_detail({
    "id": "acme/tiny-gguf",
    "pipeline_tag": "text-generation",
    "library_name": "gguf",
    "downloads": 5,
    "tags": ["gguf"],
    "siblings": [
        {"rfilename": "model.gguf", "size": 900},
        {"rfilename": "README.md", "size": 10},
    ],
})

WHISPER_DETAIL = normalise_hf_detail({
    "id": "Systran/faster-whisper-small",
    "pipeline_tag": "automatic-speech-recognition",
    "library_name": "ctranslate2",
    "downloads": 98765,
    "tags": ["ctranslate2", "license:mit"],
    "siblings": [
        {"rfilename": "model.bin", "size": 3000},
        {"rfilename": "config.json", "size": 30},
        {"rfilename": "tokenizer.json", "size": 200},
        {"rfilename": "vocabulary.txt", "size": 50},
    ],
})

KOKORO_DETAIL = normalise_hf_detail({
    "id": "hexgrad/Kokoro-82M",
    "pipeline_tag": "text-to-speech",
    "library_name": "kokoro",
    "downloads": 45678,
    "tags": ["license:apache-2.0"],
    "cardData": {"license": "apache-2.0"},
    "siblings": [
        {"rfilename": "kokoro-v1_0.pth", "size": 5000},
        {"rfilename": "config.json", "size": 40},
        {"rfilename": "voices/af_heart.pt", "size": 500},
    ],
})

SAFETENSORS_DETAIL = normalise_hf_detail({
    "id": "acme/llama-x-8b",
    "pipeline_tag": "text-generation",
    "library_name": "transformers",
    "downloads": 123,
    "tags": ["safetensors", "license:apache-2.0"],
    "cardData": {"license": "apache-2.0"},
    "siblings": [
        {"rfilename": "model-00001-of-00002.safetensors", "size": 5000},
        {"rfilename": "model-00002-of-00002.safetensors", "size": 4000},
    ],
})

DETAILS = {
    d["id"]: d
    for d in (GGUF_DETAIL, SINGLE_GGUF_DETAIL, WHISPER_DETAIL,
              KOKORO_DETAIL, SAFETENSORS_DETAIL)
}


class _StubHub:
    """Stands in for ModelHub.detail (H1), serving canned details."""

    def __init__(self, details=None):
        self._details = DETAILS if details is None else details

    async def detail(self, source, repo_id):
        detail = self._details.get(repo_id)
        if detail is None:
            return {
                "detail": None,
                "error": f"Hugging Face has no model '{repo_id}'.",
            }
        return {"detail": detail, "error": None}


class _FakeOllama:
    """Stands in for OllamaManager: records pulls, serves inventory."""

    def __init__(self):
        self.pulls: list[str] = []
        self.downloaded_models: list[dict] = []
        self.deleted: list[str] = []
        self.running = True
        self.delete_result = True

    async def is_ollama_running(self):
        return self.running

    async def list_downloaded_models(self):
        return list(self.downloaded_models)

    async def delete_model(self, model_name):
        self.deleted.append(model_name)
        return self.delete_result

    async def download_model_task(self, model_name, destination_dir=None,
                                  task_id=None, request_id=None):
        self.pulls.append(model_name)
        yield Event(
            type=EventType.TASK_CREATED,
            request_id=request_id,
            payload={"task_id": task_id, "model": model_name,
                     "state": "running"},
        )
        yield Event(
            type=EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS,
            request_id=request_id,
            payload={"task_id": task_id, "model": model_name,
                     "download_percent": 100.0, "status": "success"},
        )
        yield Event(
            type=EventType.TASK_COMPLETED,
            request_id=request_id,
            payload={"task_id": task_id, "model": model_name},
        )


# ---------------------------------------------------------------------------
# Fake huggingface_hub (the real library is an optional voice-extra dep)
# ---------------------------------------------------------------------------


class _FakeHfApi:
    files: ClassVar[dict] = {}  # repo_id -> [(filename, size)]

    def __init__(self, *args, **kwargs):
        pass

    def model_info(self, repo_id, files_metadata=False):
        siblings = [
            SimpleNamespace(rfilename=name, size=size)
            for name, size in type(self).files[repo_id]
        ]
        return SimpleNamespace(siblings=siblings)


def _make_fake_hf(cache_dir, files_by_repo):
    """A module-shaped fake: HfApi + hf_hub_download + constants."""
    _FakeHfApi.files = files_by_repo
    fake = SimpleNamespace()
    fake.HfApi = _FakeHfApi
    fake.constants = SimpleNamespace(HF_HUB_CACHE=str(cache_dir))
    fake.calls: list = []

    def hf_hub_download(repo_id=None, filename=None, cache_dir=None, **kw):
        fake.calls.append((repo_id, filename, cache_dir))
        return str(cache_dir)

    fake.hf_hub_download = hf_hub_download
    return fake


def _downloads(tmp_path, *, ollama=None, hf_loader=None, hub=None):
    return ModelHubDownloads(
        hub=hub or _StubHub(),
        ollama=ollama or _FakeOllama(),
        hf_cache_dir=tmp_path / "hf-cache",
        hf_loader=hf_loader or (lambda: None),
    )


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_hub_download_contracts_registered():
    assert EventType.MODEL_HUB_DOWNLOAD_REQUEST.value == "MODEL_HUB_DOWNLOAD_REQUEST"
    assert EventType.MODEL_HUB_DOWNLOAD_RESPONSE.value == "MODEL_HUB_DOWNLOAD_RESPONSE"
    assert EventType.MODEL_HUB_DOWNLOADED_REQUEST.value == "MODEL_HUB_DOWNLOADED_REQUEST"
    assert EventType.MODEL_HUB_DOWNLOADED_RESPONSE.value == "MODEL_HUB_DOWNLOADED_RESPONSE"
    assert EventType.MODEL_HUB_DELETE_REQUEST.value == "MODEL_HUB_DELETE_REQUEST"
    assert EventType.MODEL_HUB_DELETE_RESPONSE.value == "MODEL_HUB_DELETE_RESPONSE"


# ---------------------------------------------------------------------------
# The hf.co ref helper
# ---------------------------------------------------------------------------


def test_ref_uses_chosen_quant_as_tag():
    ref, error = hf_gguf_ollama_ref(
        GGUF_DETAIL["id"], GGUF_DETAIL["variants"], "Q8_0")
    assert error is None
    assert ref == "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0"


def test_ref_quant_match_is_case_insensitive_and_accepts_filename():
    ref, _ = hf_gguf_ollama_ref(
        GGUF_DETAIL["id"], GGUF_DETAIL["variants"], "q4_k_m")
    assert ref == "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M"
    ref2, _ = hf_gguf_ollama_ref(
        GGUF_DETAIL["id"], GGUF_DETAIL["variants"],
        "Llama-3.2-3B-Instruct-Q8_0.gguf")
    assert ref2 == "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0"


def test_ref_without_variant_and_several_quants_refuses_with_choices():
    ref, error = hf_gguf_ollama_ref(
        GGUF_DETAIL["id"], GGUF_DETAIL["variants"], None)
    assert ref is None
    assert "Q4_K_M" in error and "Q8_0" in error


def test_ref_unknown_variant_refuses_with_choices():
    ref, error = hf_gguf_ollama_ref(
        GGUF_DETAIL["id"], GGUF_DETAIL["variants"], "Q2_K")
    assert ref is None
    assert "Q2_K" in error and "Q4_K_M" in error


def test_ref_single_unquantised_gguf_is_a_bare_ref():
    ref, error = hf_gguf_ollama_ref(
        SINGLE_GGUF_DETAIL["id"], SINGLE_GGUF_DETAIL["variants"], None)
    assert error is None
    assert ref == "hf.co/acme/tiny-gguf"


def test_ref_no_gguf_variant_refuses():
    ref, error = hf_gguf_ollama_ref(
        WHISPER_DETAIL["id"], WHISPER_DETAIL["variants"], None)
    assert ref is None
    assert "GGUF" in error


# ---------------------------------------------------------------------------
# Routing (begin)
# ---------------------------------------------------------------------------


async def test_begin_ollama_source_pulls_by_name(tmp_path):
    ollama = _FakeOllama()
    mgr = _downloads(tmp_path, ollama=ollama)
    plan = await mgr.begin(source="ollama", model_id="llama3:8b")
    assert plan["success"] is True
    assert plan["store"] == "ollama"
    assert plan["kind"] == "ollama-pull"
    assert plan["key"] == "llama3:8b"
    events = [ev async for ev in mgr.run(plan)]
    assert ollama.pulls == ["llama3:8b"]
    assert events[-1].type == EventType.TASK_COMPLETED
    # the store slot was released by run()
    plan2 = await mgr.begin(source="ollama", model_id="mistral:7b")
    assert plan2["success"] is True
    mgr.release(plan2)


async def test_begin_hf_gguf_routes_to_ollama_pull_of_hf_ref(tmp_path):
    ollama = _FakeOllama()
    mgr = _downloads(tmp_path, ollama=ollama)
    plan = await mgr.begin(
        source="hf", model_id=GGUF_DETAIL["id"], variant="Q4_K_M")
    assert plan["success"] is True
    assert plan["store"] == "ollama"
    events = [ev async for ev in mgr.run(plan)]
    assert ollama.pulls == ["hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M"]
    assert events[-1].type == EventType.TASK_COMPLETED


async def test_begin_hf_gguf_without_variant_refuses_when_ambiguous(tmp_path):
    mgr = _downloads(tmp_path)
    plan = await mgr.begin(source="hf", model_id=GGUF_DETAIL["id"])
    assert plan["success"] is False
    assert "Q4_K_M" in plan["error"]


async def test_begin_not_runnable_refuses_with_h1_reason(tmp_path):
    mgr = _downloads(tmp_path)
    plan = await mgr.begin(source="hf", model_id=SAFETENSORS_DETAIL["id"])
    assert plan["success"] is False
    assert "serving stack" in plan["error"]


async def test_begin_unknown_repo_refuses_with_detail_error(tmp_path):
    mgr = _downloads(tmp_path)
    plan = await mgr.begin(source="hf", model_id="acme/nope")
    assert plan["success"] is False
    assert "acme/nope" in plan["error"]


async def test_begin_unknown_source_refuses(tmp_path):
    mgr = _downloads(tmp_path)
    plan = await mgr.begin(source="goggle", model_id="x")
    assert plan["success"] is False
    assert "goggle" in plan["error"]


async def test_begin_busy_store_refuses_second_download(tmp_path):
    cache = tmp_path / "hf-cache"
    fake_hf = _make_fake_hf(cache, {WHISPER_DETAIL["id"]: [("model.bin", 3)]})
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    first = await mgr.begin(source="ollama", model_id="llama3:8b")
    assert first["success"] is True
    second = await mgr.begin(source="ollama", model_id="mistral:7b")
    assert second["success"] is False
    assert "already in progress" in second["error"]
    # ... but the OTHER store is free: an HF speech download may start
    speech = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert speech["success"] is True
    # and the HF store is now busy too
    speech2 = await mgr.begin(source="hf", model_id=KOKORO_DETAIL["id"])
    assert speech2["success"] is False
    assert "already in progress" in speech2["error"]
    mgr.release(speech)
    mgr.release(first)


# ---------------------------------------------------------------------------
# HF speech downloads (huggingface_hub mocked; no network)
# ---------------------------------------------------------------------------


async def test_hf_speech_download_fetches_every_file_with_progress(tmp_path):
    cache = tmp_path / "hf-cache"
    files = [("model.bin", 3000), ("config.json", 30), ("tokenizer.json", 200)]
    fake_hf = _make_fake_hf(cache, {WHISPER_DETAIL["id"]: files})
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    plan = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert plan["success"] is True
    assert plan["kind"] == "hf-speech"
    events = [ev async for ev in mgr.run(plan)]

    assert events[0].type == EventType.TASK_CREATED
    assert events[-1].type == EventType.TASK_COMPLETED
    assert events[-1].payload["model"] == WHISPER_DETAIL["id"]
    # every file fetched through the library, into the hub cache root
    assert [c[1] for c in fake_hf.calls] == [f[0] for f in files]
    assert all(c[0] == WHISPER_DETAIL["id"] for c in fake_hf.calls)
    assert all(c[2] == str(cache) for c in fake_hf.calls)
    # byte-accurate progress: file sizes 3000/30/200 of 3230 total
    progress = [
        e.payload["download_percent"]
        for e in events if e.type == EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS
    ]
    assert progress[0] == 0.0
    assert progress[-1] == 100.0
    assert progress == sorted(progress)


async def test_hf_speech_download_kokoro_routes_in_app(tmp_path):
    cache = tmp_path / "hf-cache"
    files = [("kokoro-v1_0.pth", 5000), ("config.json", 40)]
    fake_hf = _make_fake_hf(cache, {KOKORO_DETAIL["id"]: files})
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    plan = await mgr.begin(source="hf", model_id=KOKORO_DETAIL["id"])
    assert plan["success"] is True
    assert plan["kind"] == "hf-speech"
    events = [ev async for ev in mgr.run(plan)]
    assert events[-1].type == EventType.TASK_COMPLETED


async def test_hf_speech_download_without_voice_components_refuses(tmp_path):
    # hf_loader -> None mirrors importlib failing: huggingface_hub
    # ships with the voice extra (faster-whisper), absent here.
    mgr = _downloads(tmp_path, hf_loader=lambda: None)
    plan = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert plan["success"] is False
    assert "not installed" in plan["error"]
    # no slot was claimed: the refusal is repeatable, not "busy"
    again = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert again["success"] is False
    assert "not installed" in again["error"]


async def test_hf_speech_download_failure_is_a_failed_task(tmp_path):
    cache = tmp_path / "hf-cache"
    fake_hf = _make_fake_hf(cache, {WHISPER_DETAIL["id"]: [("model.bin", 3)]})

    def boom(repo_id=None, filename=None, cache_dir=None, **kw):
        raise RuntimeError("network went away")

    fake_hf.hf_hub_download = boom
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    plan = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    events = [ev async for ev in mgr.run(plan)]
    failed = [e for e in events if e.type == EventType.TASK_FAILED]
    assert len(failed) == 1
    assert "network went away" in failed[0].payload["error"]
    # the failure released the store slot
    nxt = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert nxt["success"] is True
    mgr.release(nxt)


async def test_hf_speech_download_cancels_between_files(tmp_path):
    cache = tmp_path / "hf-cache"
    files = [("a.bin", 10), ("b.bin", 10), ("c.bin", 10)]
    fake_hf = _make_fake_hf(cache, {WHISPER_DETAIL["id"]: files})
    gate = threading.Event()
    second_file_started = threading.Event()

    def gated_download(repo_id=None, filename=None, cache_dir=None, **kw):
        fake_hf.calls.append((repo_id, filename, cache_dir))
        if filename == "b.bin":
            second_file_started.set()
            gate.wait(timeout=30)
        return str(cache_dir)

    fake_hf.hf_hub_download = gated_download
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    plan = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])

    collected = []

    async def consume():
        async for ev in mgr.run(plan):
            collected.append(ev)

    task = asyncio.create_task(consume())
    for _ in range(600):
        if second_file_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert second_file_started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()  # let the orphaned in-flight file finish in its thread
    await asyncio.sleep(0.2)
    # cancellation took effect between files: c.bin was never requested
    assert [c[1] for c in fake_hf.calls] == ["a.bin", "b.bin"]
    assert not any(e.type == EventType.TASK_COMPLETED for e in collected)
    # and the store slot is free again
    nxt = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert nxt["success"] is True
    mgr.release(nxt)


# ---------------------------------------------------------------------------
# Downloaded inventory
# ---------------------------------------------------------------------------


def _seed_hf_cache(cache: Path, repo_id: str, files: dict) -> Path:
    repo_dir = cache / ("models--" + repo_id.replace("/", "--"))
    blobs = repo_dir / "blobs"
    blobs.mkdir(parents=True)
    snapshot = repo_dir / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    for name, data in files.items():
        blob = blobs / (name.replace("/", "_") + ".blob")
        blob.write_bytes(data)
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(blob, link)
    return repo_dir


async def test_downloaded_inventory_merges_ollama_and_hf(tmp_path):
    cache = tmp_path / "hf-cache"
    _seed_hf_cache(cache, "Systran/faster-whisper-small",
                   {"model.bin": b"x" * 100, "config.json": b"{}"})
    _seed_hf_cache(cache, "hexgrad/Kokoro-82M", {"kokoro.pth": b"y" * 50})
    # a non-speech repo in the cache is NOT hub speech inventory
    _seed_hf_cache(cache, "acme/llama-x-8b", {"model.safetensors": b"z" * 999})
    # an unrelated whisper conversion IS speech inventory
    _seed_hf_cache(cache, "someone/faster-whisper-large-v3",
                   {"model.bin": b"w" * 7})
    ollama = _FakeOllama()
    ollama.downloaded_models = [
        {"name": "llama3:8b", "size_bytes": 4700, "path": "~/.ollama/models"},
    ]
    mgr = _downloads(tmp_path, ollama=ollama)
    result = await mgr.downloaded()
    assert result["error"] is None
    rows = {(r["source"], r["id"]): r for r in result["models"]}
    assert set(rows) == {
        ("ollama", "llama3:8b"),
        ("hf", "Systran/faster-whisper-small"),
        ("hf", "hexgrad/Kokoro-82M"),
        ("hf", "someone/faster-whisper-large-v3"),
    }
    for row in rows.values():
        assert row["downloaded"] is True
    assert rows[("ollama", "llama3:8b")]["size_bytes"] == 4700
    assert rows[("ollama", "llama3:8b")]["runnability"] == "ollama"
    whisper = rows[("hf", "Systran/faster-whisper-small")]
    assert whisper["family"] == "speech-to-text"
    assert whisper["runnability"] == "in-app"
    # sizes count real files once: the snapshot symlinks are not
    # double-counted on top of their blobs
    assert whisper["size_bytes"] == 102
    kokoro = rows[("hf", "hexgrad/Kokoro-82M")]
    assert kokoro["family"] == "text-to-speech"
    assert kokoro["runnability"] == "in-app"
    assert kokoro["size_bytes"] == 50


async def test_downloaded_inventory_empty_is_not_an_error(tmp_path):
    mgr = _downloads(tmp_path)
    result = await mgr.downloaded()
    assert result == {"models": [], "error": None}


# ---------------------------------------------------------------------------
# OllamaManager.list_downloaded_models (byte-accurate inventory view)
# ---------------------------------------------------------------------------


class _TagsResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _TagsClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        if url.endswith("/api/tags"):
            return _TagsResponse(200, {"models": [
                {"name": "llama3:8b", "size": 4700000000},
                {"name": "hf.co/acme/tiny-gguf:latest", "size": 900},
            ]})
        return _TagsResponse(200, {})


async def test_ollama_manager_list_downloaded_models(monkeypatch):
    monkeypatch.setattr(local_models.httpx, "AsyncClient", _TagsClient)
    mgr = local_models.OllamaManager()
    models = await mgr.list_downloaded_models()
    assert [m["name"] for m in models] == [
        "hf.co/acme/tiny-gguf:latest", "llama3:8b"]
    assert models[1]["size_bytes"] == 4700000000
    assert "path" in models[1]


async def test_ollama_manager_list_downloaded_models_daemon_down():
    # no daemon in this environment: an empty list, not an exception
    mgr = local_models.OllamaManager()
    assert await mgr.list_downloaded_models() == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_ollama_present(tmp_path):
    ollama = _FakeOllama()
    ollama.downloaded_models = [
        {"name": "llama3:8b", "size_bytes": 1, "path": ""}]
    mgr = _downloads(tmp_path, ollama=ollama)
    result = await mgr.delete(source="ollama", model_id="llama3:8b")
    assert result["success"] is True
    assert result["deleted"] is True
    assert ollama.deleted == ["llama3:8b"]


async def test_delete_ollama_absent_is_a_noop(tmp_path):
    ollama = _FakeOllama()
    mgr = _downloads(tmp_path, ollama=ollama)
    result = await mgr.delete(source="ollama", model_id="ghost:1b")
    assert result["success"] is True
    assert result["deleted"] is False
    assert result["error"] is None
    assert ollama.deleted == []  # the daemon was never asked


async def test_delete_ollama_daemon_down_is_an_honest_failure(tmp_path):
    ollama = _FakeOllama()
    ollama.running = False
    mgr = _downloads(tmp_path, ollama=ollama)
    result = await mgr.delete(source="ollama", model_id="llama3:8b")
    assert result["success"] is False
    assert "not running" in result["error"]


async def test_delete_hf_speech_removes_only_that_repo(tmp_path):
    cache = tmp_path / "hf-cache"
    victim = _seed_hf_cache(cache, "Systran/faster-whisper-small",
                            {"model.bin": b"x"})
    keeper = _seed_hf_cache(cache, "hexgrad/Kokoro-82M",
                            {"kokoro.pth": b"y"})
    mgr = _downloads(tmp_path)
    result = await mgr.delete(source="hf", model_id="Systran/faster-whisper-small")
    assert result["success"] is True
    assert result["deleted"] is True
    assert not victim.exists()
    assert keeper.exists()


async def test_delete_hf_absent_is_a_noop(tmp_path):
    mgr = _downloads(tmp_path)
    result = await mgr.delete(source="hf", model_id="Systran/faster-whisper-small")
    assert result["success"] is True
    assert result["deleted"] is False
    assert result["error"] is None


async def test_delete_unknown_source_refuses(tmp_path):
    mgr = _downloads(tmp_path)
    result = await mgr.delete(source="goggle", model_id="x")
    assert result["success"] is False
    assert "goggle" in result["error"]


# ---------------------------------------------------------------------------
# Socket contracts
# ---------------------------------------------------------------------------


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine auth isolated to tmp (the session-ws fixture law:
    the audit log path is set BEFORE initialize_auth), and the
    state's hub-download manager swapped for a fully faked one."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    ollama = _FakeOllama()
    ollama.downloaded_models = [
        {"name": "llama3:8b", "size_bytes": 4700, "path": "~/.ollama/models"},
    ]
    cache = tmp_path / "hf-cache"
    fake_hf = _make_fake_hf(cache, {
        WHISPER_DETAIL["id"]: [("model.bin", 3000), ("config.json", 30)],
    })
    mgr = ModelHubDownloads(
        hub=_StubHub(),
        ollama=ollama,
        hf_cache_dir=cache,
        hf_loader=lambda: fake_hf,
    )
    monkeypatch.setattr(engine_state, "hub_downloads", mgr)
    env = SimpleNamespace(mgr=mgr, ollama=ollama, fake_hf=fake_hf, cache=cache)
    yield env


def _recv_until(ws, msg_type, max_events=80):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == msg_type:
            return ev
    raise AssertionError(f"never received {msg_type}")


def _connect():
    return TestClient(app)


def test_ws_hub_download_ollama(ws_env):
    with _connect() as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOAD_REQUEST,
            payload={"source": "ollama", "id": "llama3:8b"},
        ).to_json())
        ack = _recv_until(ws, EventType.MODEL_HUB_DOWNLOAD_RESPONSE)
        assert ack.payload["success"] is True
        done = _recv_until(ws, EventType.TASK_COMPLETED)
    assert done.payload["model"] == "llama3:8b"
    assert ws_env.ollama.pulls == ["llama3:8b"]


def test_ws_hub_download_hf_gguf_builds_ref(ws_env):
    with _connect() as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOAD_REQUEST,
            payload={"source": "hf", "id": GGUF_DETAIL["id"],
                     "variant": "Q8_0"},
        ).to_json())
        ack = _recv_until(ws, EventType.MODEL_HUB_DOWNLOAD_RESPONSE)
        assert ack.payload["success"] is True
        _recv_until(ws, EventType.TASK_COMPLETED)
    assert ws_env.ollama.pulls == [
        "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0"]


def test_ws_hub_download_refusal_is_a_response_not_a_task(ws_env):
    with _connect() as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOAD_REQUEST,
            payload={"source": "hf", "id": SAFETENSORS_DETAIL["id"]},
        ).to_json())
        ack = _recv_until(ws, EventType.MODEL_HUB_DOWNLOAD_RESPONSE)
    assert ack.payload["success"] is False
    assert "serving stack" in ack.payload["error"]
    assert ws_env.ollama.pulls == []


def test_ws_hub_downloaded_and_delete(ws_env):
    _seed_hf_cache(ws_env.cache, "Systran/faster-whisper-small",
                   {"model.bin": b"x" * 10})
    with _connect() as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOADED_REQUEST, payload={},
        ).to_json())
        listing = _recv_until(ws, EventType.MODEL_HUB_DOWNLOADED_RESPONSE)
        ids = {(m["source"], m["id"]) for m in listing.payload["models"]}
        assert ("ollama", "llama3:8b") in ids
        assert ("hf", "Systran/faster-whisper-small") in ids

        ws.send_text(Event(
            type=EventType.MODEL_HUB_DELETE_REQUEST,
            payload={"source": "hf", "id": "Systran/faster-whisper-small"},
        ).to_json())
        deleted = _recv_until(ws, EventType.MODEL_HUB_DELETE_RESPONSE)
        assert deleted.payload["success"] is True
        assert deleted.payload["deleted"] is True

        # deleting again is an honest no-op, not an error
        ws.send_text(Event(
            type=EventType.MODEL_HUB_DELETE_REQUEST,
            payload={"source": "hf", "id": "Systran/faster-whisper-small"},
        ).to_json())
        again = _recv_until(ws, EventType.MODEL_HUB_DELETE_RESPONSE)
        assert again.payload["success"] is True
        assert again.payload["deleted"] is False
