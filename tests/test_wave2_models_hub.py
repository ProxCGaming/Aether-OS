"""Wave 2 (Builder I) — models/hub remainder: E5-D, E5-P, E5-G, E5-Q, E5-J.

Each test is written against the Pass A evidence card it names:

- E5-D [S3] HF hub delete builds its rmtree target from an unvalidated
  model_id (Windows traversal) and never checks the target is a hub
  speech repo — ``models/hub_downloads.py`` ``_hf_repo_dir``/``delete``.
- E5-P [S4] HF speech download fetches every file in the repo, not the
  model's file set — ``models/hub_downloads.py`` ``_run_hf_speech``.
- E5-G [S4] Model-hub detail cache grows without bound —
  ``models/model_hub.py`` ``_detail_cache``.
- E5-Q [S4] ``ModelHub.search()`` "Never raises" contract has a hole
  before the try — ``models/model_hub.py:471``.
- E5-J [S4] Reachability probe counts an empty stream as reachable —
  ``models/reachability.py:145-146``.

Network and daemon surfaces are faked throughout, mirroring
tests/test_model_hub_downloads.py.
"""

from types import SimpleNamespace
from typing import ClassVar

from aether_common.contracts import EventType
from aether_engine.models.hub_downloads import ModelHubDownloads
from aether_engine.models.model_hub import ModelHub, normalise_hf_detail
from aether_engine.models.reachability import ModelReachabilityChecker
from aether_engine.providers.base import StreamChunk

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
    ],
})


class _StubHub:
    def __init__(self, details):
        self._details = details

    async def detail(self, source, repo_id):
        detail = self._details.get(repo_id)
        if detail is None:
            return {"detail": None,
                    "error": f"Hugging Face has no model '{repo_id}'."}
        return {"detail": detail, "error": None}


class _FakeOllama:
    async def is_ollama_running(self):
        return True

    async def list_downloaded_models(self):
        return []

    async def delete_model(self, model_name):
        return False


class _FakeHfApi:
    files: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs):
        pass

    def model_info(self, repo_id, files_metadata=False):
        siblings = [
            SimpleNamespace(rfilename=name, size=size)
            for name, size in type(self).files[repo_id]
        ]
        return SimpleNamespace(siblings=siblings)


def _make_fake_hf(cache_dir, files_by_repo):
    _FakeHfApi.files = files_by_repo
    fake = SimpleNamespace()
    fake.HfApi = _FakeHfApi
    fake.constants = SimpleNamespace(HF_HUB_CACHE=str(cache_dir))
    fake.calls = []

    def hf_hub_download(repo_id=None, filename=None, cache_dir=None, **kw):
        fake.calls.append((repo_id, filename, cache_dir))
        return str(cache_dir)

    fake.hf_hub_download = hf_hub_download
    return fake


def _downloads(tmp_path, *, hf_loader=None, hub=None):
    return ModelHubDownloads(
        hub=hub or _StubHub({WHISPER_DETAIL["id"]: WHISPER_DETAIL}),
        ollama=_FakeOllama(),
        hf_cache_dir=tmp_path / "hf-cache",
        hf_loader=hf_loader or (lambda: None),
    )


def _seed_hf_cache(cache_root, repo_id, files):
    repo_dir = cache_root / ("models--" + repo_id.replace("/", "--"))
    repo_dir.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (repo_dir / name).write_bytes(blob)
    return repo_dir


# ---------------------------------------------------------------------------
# E5-D — HF delete target validation
# ---------------------------------------------------------------------------


async def test_e5d_delete_refuses_non_speech_hf_directory(tmp_path):
    """A ``models--*`` directory that is not hub speech inventory must
    not be removable through the hub delete: pre-fix, delete() rmtree'd
    any such directory the id resolved to."""
    cache = tmp_path / "hf-cache"
    stranger = _seed_hf_cache(cache, "acme/llama-x-8b", {"model.bin": b"x"})
    mgr = _downloads(tmp_path)

    result = await mgr.delete(source="hf", model_id="acme/llama-x-8b")

    assert result["success"] is False
    assert result["deleted"] is False
    assert stranger.exists()


async def test_e5d_delete_refuses_traversal_shaped_id(tmp_path):
    """A traversal-shaped id is refused outright, not resolved into a
    path: on Windows ``..\\..\\victim`` segments are separators and the
    pre-fix target escaped the cache root."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    mgr = _downloads(tmp_path)

    result = await mgr.delete(source="hf", model_id="..\\..\\victim")

    assert result["success"] is False
    assert result["deleted"] is False
    assert (victim / "keep.txt").exists()


def test_e5d_repo_dir_name_has_no_windows_separators(tmp_path):
    """The computed cache dirname is a single safe segment under BOTH
    separator conventions — a backslash must never survive into the
    rmtree target's name."""
    mgr = _downloads(tmp_path)
    for hostile in ("..\\..\\victim", "..\\victim", "a\\b", "x/../../y"):
        target = mgr._hf_repo_dir(hostile)
        assert "\\" not in target.name
        assert target.parent == mgr._hf_cache_root()


async def test_e5d_speech_delete_still_works(tmp_path):
    """Guard: the legitimate speech-repo delete is unchanged."""
    cache = tmp_path / "hf-cache"
    repo_dir = _seed_hf_cache(
        cache, "Systran/faster-whisper-small", {"model.bin": b"x"})
    mgr = _downloads(tmp_path)

    result = await mgr.delete(
        source="hf", model_id="Systran/faster-whisper-small")

    assert result["success"] is True
    assert result["deleted"] is True
    assert not repo_dir.exists()


# ---------------------------------------------------------------------------
# E5-P — HF speech download fetches the model's file set
# ---------------------------------------------------------------------------


async def test_e5p_download_skips_duplicate_weight_formats(tmp_path):
    """A faster-whisper repo carrying duplicate weight formats the
    in-app runtime never reads (safetensors / PyTorch copies next to
    the CTranslate2 model.bin) must download only the model's file
    set: model.bin + its companions."""
    cache = tmp_path / "hf-cache"
    files = [
        ("model.bin", 3000),
        ("config.json", 30),
        ("tokenizer.json", 200),
        ("vocabulary.txt", 50),
        ("model.safetensors", 9000),
        ("pytorch_model.bin", 9000),
    ]
    fake_hf = _make_fake_hf(cache, {WHISPER_DETAIL["id"]: files})
    mgr = _downloads(tmp_path, hf_loader=lambda: fake_hf)
    plan = await mgr.begin(source="hf", model_id=WHISPER_DETAIL["id"])
    assert plan["success"] is True

    events = [ev async for ev in mgr.run(plan)]

    assert events[-1].type == EventType.TASK_COMPLETED
    fetched = [c[1] for c in fake_hf.calls]
    assert fetched == ["model.bin", "config.json", "tokenizer.json",
                       "vocabulary.txt"]
    progress = [
        e.payload["download_percent"]
        for e in events if e.type == EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS
    ]
    assert progress[-1] == 100.0


# ---------------------------------------------------------------------------
# E5-G — detail cache is bounded
# ---------------------------------------------------------------------------


def _detail_payload(repo_id):
    return {"id": repo_id, "pipeline_tag": "text-generation",
            "siblings": []}


async def test_e5g_detail_cache_is_bounded(monkeypatch):
    hub = ModelHub()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return _detail_payload(path.rsplit("/", 1)[-1])

    monkeypatch.setattr(hub, "_hf_get", fake_get)
    total = 250
    for i in range(total):
        result = await hub.detail("hf", f"acme/model-{i}")
        assert result["error"] is None

    assert len(hub._detail_cache) <= 128
    # The most recent entry survived eviction and is served from cache.
    calls.clear()
    again = await hub.detail("hf", f"acme/model-{total - 1}")
    assert again["error"] is None
    assert calls == []


async def test_e5g_expired_entries_do_not_accumulate(monkeypatch):
    hub = ModelHub(cache_ttl=0.0)  # every entry is born expired

    async def fake_get(path, params=None):
        return _detail_payload(path.rsplit("/", 1)[-1])

    monkeypatch.setattr(hub, "_hf_get", fake_get)
    for i in range(40):
        await hub.detail("hf", f"acme/model-{i}")
    # Expired entries are evicted rather than piling up re-visits.
    assert len(hub._detail_cache) <= 1


# ---------------------------------------------------------------------------
# E5-Q — search() never raises (comment-is-contract)
# ---------------------------------------------------------------------------


async def test_e5q_search_with_non_int_limit_returns_error_payload():
    hub = ModelHub()
    result = await hub.search("hf", limit="not-a-number")
    assert result["models"] == []
    assert result["error"]


async def test_e5q_search_with_none_limit_returns_error_payload():
    hub = ModelHub()
    result = await hub.search("hf", limit=None)
    assert result["models"] == []
    assert result["error"]


async def test_e5q_search_with_string_number_limit_still_searches(monkeypatch):
    """Guard: a numeric string limit is coerced, not refused, and the
    search itself is otherwise unchanged."""
    hub = ModelHub()
    seen = {}

    async def fake_get(path, params=None):
        seen.update(params or {})
        return []

    monkeypatch.setattr(hub, "_hf_get", fake_get)
    result = await hub.search("hf", limit="5")
    assert result == {"models": [], "error": None}
    assert seen["limit"] == 5


# ---------------------------------------------------------------------------
# E5-J — an empty probe stream is not "reachable"
# ---------------------------------------------------------------------------


def _patch_call_stream(monkeypatch, chunks):
    from aether_engine.providers.litellm_provider import LiteLLMProvider

    async def fake_call_stream(self, messages, **kwargs):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(LiteLLMProvider, "call_stream", fake_call_stream)


async def test_e5j_empty_stream_is_not_reachable(tmp_path, monkeypatch):
    checker = ModelReachabilityChecker(db_path=tmp_path / "reach.db")
    _patch_call_stream(monkeypatch, [])

    result = await checker.check_single_model(
        "google_gemini", "some-model", "fake-key", force=True)

    assert result is False
    cached = checker.get_cached_reachability("google_gemini", "some-model")
    assert cached is not None
    assert cached[0] is False


async def test_e5j_nonempty_stream_is_reachable(tmp_path, monkeypatch):
    """Guard: one real chunk still records reachable."""
    checker = ModelReachabilityChecker(db_path=tmp_path / "reach.db")
    _patch_call_stream(monkeypatch, [StreamChunk(text="hi")])

    result = await checker.check_single_model(
        "google_gemini", "some-model", "fake-key", force=True)

    assert result is True
