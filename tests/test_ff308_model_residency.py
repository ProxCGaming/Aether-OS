"""FF3-08 residency: loaded state from /api/ps + unload (Spec D completion).

The daemon fake is scripted per test: /api/tags names two models,
/api/ps reports one of them resident. Residency is a REPORTED fact:
when /api/ps cannot answer, rows carry no `loaded` key at all —
an unreported state is never rendered as a reported false.
"""
import pytest

from aether_common.contracts import EventType
from aether_engine.models import local_models
from aether_engine.models.local_models import OllamaManager


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


def _make_client(*, ps_status=200, generate_status=200, captured=None):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            if url.endswith("/api/tags"):
                return _Resp(200, {"models": [
                    {"name": "alpha:latest", "size": 100, "modified_at": "t1"},
                    {"name": "beta:latest", "size": 200, "modified_at": "t2"},
                ]})
            if url.endswith("/api/ps"):
                if ps_status != 200:
                    return _Resp(ps_status, {})
                return _Resp(200, {"models": [{"name": "alpha:latest"}]})
            return _Resp(200, {})

        async def post(self, url, json=None):
            if url.endswith("/api/show"):
                return _Resp(200, {"capabilities": ["completion"], "details": {}})
            if url.endswith("/api/generate"):
                if captured is not None:
                    captured.append(json)
                return _Resp(generate_status, {})
            return _Resp(200, {})

    return _Client


@pytest.fixture
def mgr():
    return OllamaManager()


class TestLoadedFlags:
    @pytest.mark.asyncio
    async def test_list_models_marks_residency(self, mgr, monkeypatch):
        monkeypatch.setattr(local_models.httpx, "AsyncClient", _make_client())
        models = await mgr.list_models()
        by_name = {m["name"]: m for m in models if m.get("downloaded")}
        assert by_name["alpha:latest"]["loaded"] is True
        assert by_name["beta:latest"]["loaded"] is False

    @pytest.mark.asyncio
    async def test_unanswerable_ps_omits_loaded(self, mgr, monkeypatch):
        monkeypatch.setattr(
            local_models.httpx, "AsyncClient", _make_client(ps_status=500))
        models = await mgr.list_models()
        downloaded = [m for m in models if m.get("downloaded")]
        assert downloaded
        assert all("loaded" not in m for m in downloaded)

    @pytest.mark.asyncio
    async def test_downloaded_inventory_marks_residency(self, mgr, monkeypatch):
        monkeypatch.setattr(local_models.httpx, "AsyncClient", _make_client())
        models = await mgr.list_downloaded_models()
        by_name = {m["name"]: m for m in models}
        assert by_name["alpha:latest"]["loaded"] is True
        assert by_name["beta:latest"]["loaded"] is False


class TestUnload:
    @pytest.mark.asyncio
    async def test_unload_posts_keep_alive_zero(self, mgr, monkeypatch):
        captured = []
        monkeypatch.setattr(
            local_models.httpx, "AsyncClient", _make_client(captured=captured))
        assert await mgr.unload_model("alpha:latest") is True
        assert captured == [{"model": "alpha:latest", "keep_alive": 0}]

    @pytest.mark.asyncio
    async def test_unload_non_200_is_false(self, mgr, monkeypatch):
        monkeypatch.setattr(
            local_models.httpx, "AsyncClient",
            _make_client(generate_status=500))
        assert await mgr.unload_model("alpha:latest") is False


def test_unload_contract_pair_exists():
    assert EventType.LOCAL_MODEL_UNLOAD_REQUEST.value == "LOCAL_MODEL_UNLOAD_REQUEST"
    assert EventType.LOCAL_MODEL_UNLOAD_RESPONSE.value == "LOCAL_MODEL_UNLOAD_RESPONSE"
