"""Phase 10.3 B2: reachability total-failure honesty + dead router removal.

- ``filter_reachable_models`` must return an empty list with a real error
  (discovery-v2 ``DiscoveryError`` shape) when nothing is reachable, never
  the full input list as a silent success.
- ``CapabilityRouter`` was deleted (no live caller; the live routing path
  is ``RoutingPolicy.select_route``). No ``.py`` file may import it anymore.
"""
import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest
from unittest.mock import patch

from aether_engine.models.reachability import ModelReachabilityChecker, ReachableModels
from aether_engine.providers.discovery import DiscoveryError

_MODELS = ["gemini-good-1", "gemini-bad-2", "gemini-good-3"]


def _checker(tmp_path):
    return ModelReachabilityChecker(db_path=tmp_path / "test_reachability.db")


@pytest.mark.asyncio
async def test_zero_reachable_returns_empty_list_with_error(tmp_path):
    """Failed before the fix: the full input list came back as if reachable."""
    checker = _checker(tmp_path)

    async def _all_unreachable(provider_name, model_id, api_key, **kwargs):
        return False

    with patch.object(checker, "check_single_model", side_effect=_all_unreachable):
        result = await checker.filter_reachable_models("google_gemini", _MODELS, "fake-key")

    # Empty list, still a plain list for existing callers (capability_jobs
    # Option C relies on truthiness + iteration of the return value).
    assert isinstance(result, list)
    assert result == []
    assert list(result) == []
    assert not result
    assert isinstance(result, ReachableModels)
    assert result.ok is False
    assert isinstance(result.error, DiscoveryError)
    assert "google_gemini" in result.error.message
    assert "probed 3, 0 reachable" in result.error.message


@pytest.mark.asyncio
async def test_no_api_key_returns_empty_list_with_error(tmp_path):
    """No key means nothing could be probed — not a licence to return all models."""
    checker = _checker(tmp_path)
    result = await checker.filter_reachable_models("google_gemini", _MODELS, "")
    assert result == []
    assert isinstance(result.error, DiscoveryError)
    assert "API key" in result.error.message
    assert result.ok is False


@pytest.mark.asyncio
async def test_empty_input_returns_empty_without_error(tmp_path):
    """Nothing to probe is not a failure — empty in, empty out, no error."""
    checker = _checker(tmp_path)
    result = await checker.filter_reachable_models("google_gemini", [], "fake-key")
    assert result == []
    assert result.error is None
    assert result.ok is True


@pytest.mark.asyncio
async def test_some_reachable_returns_only_reachable_without_error(tmp_path):
    checker = _checker(tmp_path)

    async def _mock_check(provider_name, model_id, api_key, **kwargs):
        return "good" in model_id

    with patch.object(checker, "check_single_model", side_effect=_mock_check):
        result = await checker.filter_reachable_models("google_gemini", _MODELS, "fake-key")

    assert result == ["gemini-good-1", "gemini-good-3"]
    assert result.error is None
    assert result.ok is True


def test_no_import_of_deleted_router_remains():
    """Repo-wide grep: no Python file may reference the deleted router.

    Excludes this test file itself (it names the deleted module in its
    own assertions) and generated/venv trees. Historical prose in docs,
    ADRs and CHANGELOG is out of scope — those record past work, they
    are not imports.
    """
    repo_root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"capability_router|CapabilityRouter")
    offenders = []
    for path in repo_root.rglob("*.py"):
        if any(part.startswith(".venv") or part == "__pycache__" for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []


def test_deleted_router_module_is_not_importable():
    sys.modules.pop("aether_engine.routing.capability_router", None)
    assert importlib.util.find_spec("aether_engine.routing.capability_router") is None
    with pytest.raises(ImportError):
        importlib.import_module("aether_engine.routing.capability_router")
