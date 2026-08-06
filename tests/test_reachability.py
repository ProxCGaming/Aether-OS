import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aether_engine.models.reachability import ModelReachabilityChecker

@pytest.mark.asyncio
async def test_reachability_checker_caching(tmp_path):
    db_path = tmp_path / "test_reachability.db"
    checker = ModelReachabilityChecker(db_path=db_path)

    with patch("aether_engine.providers.litellm_provider.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(delta=MagicMock(content="hi"))]
        async def _stream():
            yield mock_resp
        mock_acompletion.return_value = _stream()

        # First probe - should call acompletion
        result = await checker.check_single_model("google_gemini", "gemini-1.5-pro", "fake-key")
        assert result is True
        assert mock_acompletion.call_count == 1

        # Second probe - should hit cache
        result_cached = await checker.check_single_model("google_gemini", "gemini-1.5-pro", "fake-key")
        assert result_cached is True
        assert mock_acompletion.call_count == 1

@pytest.mark.asyncio
async def test_reachability_checker_unreachable_on_error(tmp_path):
    db_path = tmp_path / "test_reachability.db"
    checker = ModelReachabilityChecker(db_path=db_path)

    with patch("aether_engine.providers.litellm_provider.litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.side_effect = Exception("Model not found 404")

        result = await checker.check_single_model("google_gemini", "deprecated-model", "fake-key")
        assert result is False

@pytest.mark.asyncio
async def test_reachability_filter_models(tmp_path):
    db_path = tmp_path / "test_reachability.db"
    checker = ModelReachabilityChecker(db_path=db_path)

    async def mock_check_single(provider_name, model_id, api_key, **kwargs):
        return "good" in model_id

    with patch.object(checker, "check_single_model", side_effect=mock_check_single):
        models = ["gemini-good-1", "gemini-bad-2", "gemini-good-3"]
        reachable = await checker.filter_reachable_models("google_gemini", models, "fake-key")
        assert reachable == ["gemini-good-1", "gemini-good-3"]

