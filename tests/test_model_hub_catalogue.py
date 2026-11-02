"""Model hub catalogue layer (H1) — HF + Ollama sources, read-only.

Owner design (speech-model-picker grill, hub scope decided 2 Nov
2026): the Local Models tab becomes a model hub. Sources are
Hugging Face (live public API) and the Ollama library ONLY; the
local runtime is Ollama only. Every model is labelled by HOW IT
RUNS: "in-app" (speech — faster-whisper/Kokoro class), "ollama"
(GGUF brains), "not-runnable" (weights needing a serving stack the
app doesn't carry), or "unknown" (never guess "runnable").

These tests pin: the HF search-row normaliser, the pure
runnability labeller, GGUF quant parsing, detail variant grouping,
the hub's HTTP behaviour with httpx mocked at the boundary (no
real network), the shipped Ollama catalogue as hub rows, and the
MODEL_HUB_* socket contracts. Offline/error paths must yield an
honest error payload — never an exception, never fabricated rows.
"""

import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.models import model_hub
from aether_engine.models.model_hub import (
    ModelHub,
    family_for_pipeline_tag,
    formats_from_files,
    label_runnability,
    normalise_hf_detail,
    normalise_hf_search_row,
    ollama_catalogue_rows,
    parse_gguf_quant,
)

# ---------------------------------------------------------------------------
# Fixture HF payloads (shapes mirror the public /api/models responses)
# ---------------------------------------------------------------------------

HF_SEARCH_PAYLOAD = [
    {
        "id": "acme/llama-x-8b",
        "pipeline_tag": "text-generation",
        "library_name": "transformers",
        "downloads": 123456,
        "tags": ["transformers", "safetensors", "llama", "license:apache-2.0"],
        "cardData": {"license": "apache-2.0"},
    },
    {
        "id": "acme/llama-x-8b-gguf",
        "pipeline_tag": "text-generation",
        "library_name": "gguf",
        "downloads": 54321,
        "tags": ["gguf", "license:apache-2.0"],
        "cardData": {"license": ["apache-2.0"]},
    },
    {
        "id": "Systran/faster-whisper-small",
        "pipeline_tag": "automatic-speech-recognition",
        "library_name": "ctranslate2",
        "downloads": 98765,
        "tags": ["ctranslate2", "license:mit"],
    },
    {
        "id": "hexgrad/Kokoro-82M",
        "pipeline_tag": "text-to-speech",
        "library_name": "kokoro",
        "downloads": 45678,
        "tags": ["license:apache-2.0"],
        "cardData": {"license": "apache-2.0"},
    },
    {
        "id": "acme/embed-mini",
        "pipeline_tag": "sentence-similarity",
        "library_name": "sentence-transformers",
        "downloads": 22222,
        "tags": ["safetensors"],
    },
    {
        "id": "acme/vision-thing",
        "pipeline_tag": "image-classification",
        "downloads": 111,
        "tags": [],
    },
]

HF_GGUF_DETAIL = {
    "id": "acme/llama-x-8b-gguf",
    "pipeline_tag": "text-generation",
    "library_name": "gguf",
    "downloads": 54321,
    "tags": ["gguf"],
    "cardData": {"license": "apache-2.0"},
    "siblings": [
        {"rfilename": "llama-x-8b-Q4_K_M.gguf", "size": 4900},
        {"rfilename": "llama-x-8b-Q8_0.gguf", "size": 8500},
        {"rfilename": "README.md", "size": 120},
        {"rfilename": "config.json", "size": 40},
    ],
}

HF_CT2_DETAIL = {
    "id": "someone/whisper-large-ct2",
    "pipeline_tag": "automatic-speech-recognition",
    "library_name": "ctranslate2",
    "downloads": 777,
    "tags": ["ctranslate2"],
    "siblings": [
        {"rfilename": "model.bin", "size": 3000},
        {"rfilename": "config.json", "size": 30},
        {"rfilename": "tokenizer.json", "size": 200},
        {"rfilename": "vocabulary.txt", "size": 50},
        {"rfilename": "README.md", "size": 10},
    ],
}

HF_SAFETENSORS_DETAIL = {
    "id": "acme/llama-x-8b",
    "pipeline_tag": "text-generation",
    "library_name": "transformers",
    "downloads": 123456,
    "tags": ["safetensors"],
    "cardData": {"license": "apache-2.0"},
    "siblings": [
        {"rfilename": "model-00001-of-00002.safetensors", "size": 5000},
        {"rfilename": "model-00002-of-00002.safetensors", "size": 4000},
        {"rfilename": "model.safetensors.index.json", "size": 60},
        {"rfilename": "config.json", "size": 45},
        {"rfilename": "tokenizer.json", "size": 300},
    ],
}


# ---------------------------------------------------------------------------
# Family mapping + search-row normaliser
# ---------------------------------------------------------------------------


def test_family_mapping():
    assert family_for_pipeline_tag("text-generation") == "brain"
    assert family_for_pipeline_tag("automatic-speech-recognition") == "speech-to-text"
    assert family_for_pipeline_tag("text-to-speech") == "text-to-speech"
    assert family_for_pipeline_tag("feature-extraction") == "embedding"
    assert family_for_pipeline_tag("sentence-similarity") == "embedding"
    assert family_for_pipeline_tag("image-classification") == "other"
    assert family_for_pipeline_tag(None) == "other"
    assert family_for_pipeline_tag("") == "other"


def test_search_row_safetensors_brain_is_not_runnable():
    row = normalise_hf_search_row(HF_SEARCH_PAYLOAD[0])
    assert row["source"] == "hf"
    assert row["id"] == "acme/llama-x-8b"
    assert row["name"] == "llama-x-8b"
    assert row["family"] == "brain"
    assert row["downloads"] == 123456
    assert row["licence"] == "apache-2.0"
    assert row["formats"] == ["safetensors"]
    assert row["runnability"] == "not-runnable"
    assert "serving stack" in row["runnability_reason"]


def test_search_row_gguf_brain_runs_via_ollama():
    row = normalise_hf_search_row(HF_SEARCH_PAYLOAD[1])
    assert row["family"] == "brain"
    assert row["formats"] == ["gguf"]
    assert row["runnability"] == "ollama"
    # cardData license given as a LIST still normalises to one string
    assert row["licence"] == "apache-2.0"


def test_search_row_systran_whisper_is_in_app_and_licence_from_tag():
    row = normalise_hf_search_row(HF_SEARCH_PAYLOAD[2])
    assert row["family"] == "speech-to-text"
    assert row["runnability"] == "in-app"
    # no cardData in this fixture: licence comes from the license:* tag
    assert row["licence"] == "mit"


def test_search_row_kokoro_is_in_app():
    row = normalise_hf_search_row(HF_SEARCH_PAYLOAD[3])
    assert row["family"] == "text-to-speech"
    assert row["runnability"] == "in-app"


def test_search_row_embedding_and_other_are_honest_unknowns():
    embed = normalise_hf_search_row(HF_SEARCH_PAYLOAD[4])
    assert embed["family"] == "embedding"
    # safetensors embeddings are NOT labelled not-runnable (that rule
    # is brain-only) and never guessed runnable
    assert embed["runnability"] == "unknown"
    assert embed["licence"] is None

    other = normalise_hf_search_row(HF_SEARCH_PAYLOAD[5])
    assert other["family"] == "other"
    assert other["runnability"] == "unknown"
    assert other["formats"] == []


# ---------------------------------------------------------------------------
# Runnability labeller (pure)
# ---------------------------------------------------------------------------


def test_labeller_rules():
    # STT + CTranslate2 layout, non-Systran id → in-app
    label, _ = label_runnability(
        "speech-to-text", ["ctranslate2"], "someone/whisper-large-ct2")
    assert label == "in-app"
    # STT + Systran id, formats unknown at search level → in-app
    label, _ = label_runnability("speech-to-text", [], "Systran/faster-whisper-base")
    assert label == "in-app"
    # STT with ONLY a GGUF build: Ollama can't serve it for Aether and
    # there is no in-app GGUF speech runtime — honest unknown
    label, reason = label_runnability("speech-to-text", ["gguf"], "acme/whisper-gguf")
    assert label == "unknown"
    assert reason
    # TTS Kokoro → in-app regardless of file formats
    label, _ = label_runnability("text-to-speech", [], "hexgrad/Kokoro-82M")
    assert label == "in-app"
    # TTS non-Kokoro safetensors → unknown (not-runnable is brain-only)
    label, _ = label_runnability("text-to-speech", ["safetensors"], "acme/tts-x")
    assert label == "unknown"
    # Brain with GGUF + safetensors → GGUF wins (Ollama)
    label, _ = label_runnability("brain", ["safetensors", "gguf"], "acme/mixed")
    assert label == "ollama"
    # Embedding GGUF → Ollama serves embeddings
    label, _ = label_runnability("embedding", ["gguf"], "acme/embed-gguf")
    assert label == "ollama"
    # Brain, no recognised formats → unknown, never guessed runnable
    label, reason = label_runnability("brain", [], "acme/mystery")
    assert label == "unknown"
    assert reason


# ---------------------------------------------------------------------------
# GGUF quant parsing + detail grouping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("llama-x-8b-Q4_K_M.gguf", "Q4_K_M"),
        ("model-q8_0.gguf", "Q8_0"),
        ("model-IQ2_XS.gguf", "IQ2_XS"),
        ("UD-Q5_K_S.gguf", "Q5_K_S"),
        ("model-F16.gguf", "F16"),
        ("model-Q2_K.gguf", "Q2_K"),
        ("plain.gguf", None),
    ],
)
def test_parse_gguf_quant(filename, expected):
    assert parse_gguf_quant(filename) == expected


def test_formats_from_files():
    assert formats_from_files(["a.gguf", "b.safetensors", "model.bin", "c.onnx"]) == [
        "gguf",
        "safetensors",
        "ctranslate2",
        "onnx",
    ]
    assert formats_from_files(["README.md"]) == []


def test_detail_gguf_variants():
    detail = normalise_hf_detail(HF_GGUF_DETAIL)
    assert detail["source"] == "hf"
    assert detail["formats"] == ["gguf"]
    assert detail["runnability"] == "ollama"
    assert detail["licence"] == "apache-2.0"
    assert detail["pipeline_tag"] == "text-generation"
    gguf = [v for v in detail["variants"] if v["kind"] == "gguf"]
    assert len(gguf) == 2
    by_quant = {v["quant"]: v for v in gguf}
    assert by_quant["Q4_K_M"]["size_bytes"] == 4900
    assert by_quant["Q4_K_M"]["file"] == "llama-x-8b-Q4_K_M.gguf"
    assert by_quant["Q8_0"]["size_bytes"] == 8500
    other = [v for v in detail["variants"] if v["kind"] == "other"]
    assert len(other) == 1
    assert set(other[0]["files"]) == {"README.md", "config.json"}
    assert detail["size_bytes"] == 4900 + 8500 + 120 + 40


def test_detail_ctranslate2_variants():
    detail = normalise_hf_detail(HF_CT2_DETAIL)
    assert detail["formats"] == ["ctranslate2"]
    assert detail["runnability"] == "in-app"
    ct2 = [v for v in detail["variants"] if v["kind"] == "ctranslate2"]
    assert len(ct2) == 1
    assert set(ct2[0]["files"]) == {
        "model.bin",
        "config.json",
        "tokenizer.json",
        "vocabulary.txt",
    }
    assert ct2[0]["size_bytes"] == 3000 + 30 + 200 + 50


def test_detail_safetensors_grouped_as_one_variant():
    detail = normalise_hf_detail(HF_SAFETENSORS_DETAIL)
    assert detail["formats"] == ["safetensors"]
    assert detail["runnability"] == "not-runnable"
    st = [v for v in detail["variants"] if v["kind"] == "safetensors"]
    assert len(st) == 1
    assert st[0]["size_bytes"] == 9000
    assert len(st[0]["files"]) == 2


# ---------------------------------------------------------------------------
# Fake HF HTTP boundary (same monkeypatch idiom as the local-models tests)
# ---------------------------------------------------------------------------


class _FakeHFResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeHFClient:
    """Records calls; per-test handler decides the response/exception."""

    calls: ClassVar[list] = []
    handler: ClassVar = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, params=None):
        type(self).calls.append((url, params))
        return type(self).handler(url, params)


@pytest.fixture
def fake_hf(monkeypatch):
    _FakeHFClient.calls = []
    _FakeHFClient.handler = None
    monkeypatch.setattr(model_hub.httpx, "AsyncClient", _FakeHFClient)
    return _FakeHFClient


def _search_handler(url, params):
    return _FakeHFResponse(200, HF_SEARCH_PAYLOAD)


async def test_hub_search_hf_normalises_rows(fake_hf):
    fake_hf.handler = _search_handler
    hub = ModelHub()
    result = await hub.search(source="hf", query="llama")
    assert result["error"] is None
    assert len(result["models"]) == len(HF_SEARCH_PAYLOAD)
    assert result["models"][0]["id"] == "acme/llama-x-8b"
    # the query reaches the API as the search parameter
    _, params = fake_hf.calls[0]
    assert params["search"] == "llama"
    assert params["sort"] == "downloads"


async def test_hub_search_family_filter_passed_and_enforced(fake_hf):
    fake_hf.handler = _search_handler
    hub = ModelHub()
    result = await hub.search(source="hf", family="brain")
    _, params = fake_hf.calls[0]
    assert params["pipeline_tag"] == "text-generation"
    # client-side enforcement: only brain rows survive
    assert {m["family"] for m in result["models"]} == {"brain"}


async def test_hub_search_is_not_cached(fake_hf):
    fake_hf.handler = _search_handler
    hub = ModelHub()
    await hub.search(source="hf", query="a")
    await hub.search(source="hf", query="a")
    assert len(fake_hf.calls) == 2


async def test_hub_search_http_error_is_an_honest_payload(fake_hf):
    fake_hf.handler = lambda url, params: _FakeHFResponse(503, None)
    hub = ModelHub()
    result = await hub.search(source="hf", query="x")
    assert result["models"] == []
    assert "503" in result["error"]


async def test_hub_search_unreachable_is_an_honest_payload(fake_hf):
    def boom(url, params):
        raise httpx.ConnectError("no route")

    fake_hf.handler = boom
    hub = ModelHub()
    result = await hub.search(source="hf", query="x")
    assert result["models"] == []
    assert "unreachable" in result["error"]


async def test_hub_search_timeout_is_an_honest_payload(fake_hf):
    def slow(url, params):
        raise httpx.ReadTimeout("too slow")

    fake_hf.handler = slow
    hub = ModelHub()
    result = await hub.search(source="hf", query="x")
    assert result["models"] == []
    assert "timed out" in result["error"]


class _BoomCtorClient:
    """Client construction itself fails (the malformed-proxy-env case:
    httpx.InvalidURL is NOT an httpx.HTTPError subclass)."""

    def __init__(self, *args, **kwargs):
        raise httpx.InvalidURL("bad proxy env")


async def test_hub_client_construction_failure_is_an_honest_payload(monkeypatch):
    monkeypatch.setattr(model_hub.httpx, "AsyncClient", _BoomCtorClient)
    hub = ModelHub()
    searched = await hub.search(source="hf", query="x")
    assert searched["models"] == []
    assert "unreachable" in searched["error"]
    detailed = await hub.detail(source="hf", repo_id="acme/x")
    assert detailed["detail"] is None
    assert "unreachable" in detailed["error"]


async def test_hub_detail_hf_and_cache(fake_hf):
    def handler(url, params):
        assert "acme/llama-x-8b-gguf" in url
        return _FakeHFResponse(200, HF_GGUF_DETAIL)

    fake_hf.handler = handler
    hub = ModelHub()
    first = await hub.detail(source="hf", repo_id="acme/llama-x-8b-gguf")
    assert first["error"] is None
    assert first["detail"]["id"] == "acme/llama-x-8b-gguf"
    second = await hub.detail(source="hf", repo_id="acme/llama-x-8b-gguf")
    assert second["detail"] == first["detail"]
    # the second call was served from the brief in-memory cache
    assert len(fake_hf.calls) == 1


async def test_hub_detail_missing_repo_is_an_honest_payload(fake_hf):
    fake_hf.handler = lambda url, params: _FakeHFResponse(404, None)
    hub = ModelHub()
    result = await hub.detail(source="hf", repo_id="acme/nope")
    assert result["detail"] is None
    assert "acme/nope" in result["error"]


async def test_hub_unknown_source_is_an_honest_payload():
    hub = ModelHub()
    searched = await hub.search(source="goggle")
    assert searched["models"] == []
    assert "goggle" in searched["error"]
    detailed = await hub.detail(source="goggle", repo_id="x/y")
    assert detailed["detail"] is None
    assert "goggle" in detailed["error"]


# ---------------------------------------------------------------------------
# Ollama source (shipped catalogue; never scraped)
# ---------------------------------------------------------------------------


def test_ollama_rows_shape():
    rows = ollama_catalogue_rows()
    assert rows, "shipped catalogue must produce rows"
    for row in rows:
        assert row["source"] == "ollama"
        assert row["family"] == "brain"
        assert row["runnability"] == "ollama"
        assert row["formats"] == ["gguf"]
        # the shipped catalogue carries no licence — never invented
        assert row["licence"] is None
        assert row["size_bytes"] and row["size_bytes"] > 0
    names = {r["id"] for r in rows}
    assert "llama3:8b" in names


def test_ollama_fallback_matches_shipped_json():
    json_path = (
        Path(__file__).resolve().parents[1]
        / "desktop_app"
        / "public"
        / "ollama-catalog.json"
    )
    if not json_path.is_file():
        pytest.skip("shipped catalogue JSON not present in this checkout")
    shipped = json.loads(json_path.read_text(encoding="utf-8"))
    fallback = {e["name"]: e for e in model_hub._FALLBACK_OLLAMA_CATALOG}
    assert {e["name"] for e in shipped} == set(fallback)
    for entry in shipped:
        assert fallback[entry["name"]]["size"] == entry["size"]
        assert fallback[entry["name"]]["quant"] == entry["quant"]


async def test_hub_search_ollama_filters_locally():
    hub = ModelHub()
    result = await hub.search(source="ollama", query="llama")
    assert result["error"] is None
    assert [m["id"] for m in result["models"]] == ["llama3:8b"]
    empty = await hub.search(source="ollama", family="speech-to-text")
    assert empty["models"] == []
    assert empty["error"] is None


async def test_hub_detail_ollama():
    hub = ModelHub()
    result = await hub.detail(source="ollama", repo_id="llama3:8b")
    assert result["error"] is None
    detail = result["detail"]
    assert detail["source"] == "ollama"
    assert detail["runnability"] == "ollama"
    gguf = [v for v in detail["variants"] if v["kind"] == "gguf"]
    assert len(gguf) == 1
    assert gguf[0]["quant"] == "Q4_K_M"
    missing = await hub.detail(source="ollama", repo_id="not:real")
    assert missing["detail"] is None
    assert missing["error"]


# ---------------------------------------------------------------------------
# Socket contracts
# ---------------------------------------------------------------------------


def test_hub_contracts_registered():
    assert EventType.MODEL_HUB_SEARCH_REQUEST.value == "MODEL_HUB_SEARCH_REQUEST"
    assert EventType.MODEL_HUB_SEARCH_RESPONSE.value == "MODEL_HUB_SEARCH_RESPONSE"
    assert EventType.MODEL_HUB_DETAIL_REQUEST.value == "MODEL_HUB_DETAIL_REQUEST"
    assert EventType.MODEL_HUB_DETAIL_RESPONSE.value == "MODEL_HUB_DETAIL_RESPONSE"


@pytest.fixture
def ws_env(tmp_path):
    """Engine auth isolated to tmp (the session-ws fixture law:
    the audit log path is set BEFORE initialize_auth)."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    yield


def _recv_until(ws, msg_type, max_events=60):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == msg_type:
            return ev
    raise AssertionError(f"never received {msg_type}")


def test_ws_hub_search_ollama(ws_env):
    with TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(
            Event(
                type=EventType.MODEL_HUB_SEARCH_REQUEST,
                payload={"source": "ollama", "query": "mistral"},
            ).to_json()
        )
        ev = _recv_until(ws, EventType.MODEL_HUB_SEARCH_RESPONSE)
    assert ev.payload["error"] is None
    assert [m["id"] for m in ev.payload["models"]] == ["mistral:7b"]
    assert ev.payload["models"][0]["runnability"] == "ollama"


def test_ws_hub_search_hf_offline_is_honest(ws_env, monkeypatch):
    def boom(url, params):
        raise httpx.ConnectError("no route")

    _FakeHFClient.calls = []
    _FakeHFClient.handler = boom
    monkeypatch.setattr(model_hub.httpx, "AsyncClient", _FakeHFClient)
    with TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(
            Event(
                type=EventType.MODEL_HUB_SEARCH_REQUEST,
                payload={"source": "hf", "query": "whisper"},
            ).to_json()
        )
        ev = _recv_until(ws, EventType.MODEL_HUB_SEARCH_RESPONSE)
    assert ev.payload["models"] == []
    assert "unreachable" in ev.payload["error"]


def test_ws_hub_detail_ollama(ws_env):
    with TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.send_text(
            Event(
                type=EventType.MODEL_HUB_DETAIL_REQUEST,
                payload={"source": "ollama", "id": "llama3:8b"},
            ).to_json()
        )
        ev = _recv_until(ws, EventType.MODEL_HUB_DETAIL_RESPONSE)
    assert ev.payload["error"] is None
    assert ev.payload["detail"]["id"] == "llama3:8b"
