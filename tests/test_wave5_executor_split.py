"""Wave 5 craft: executor's manifest tracker + run-config helpers split.

``langgraph.executor`` used to bury two independent jobs among the
run/resume coroutines: the rewind manifest tracker (now
``langgraph.manifest``) and the run-config snapshot / history-repair
helpers (now ``langgraph.run_config``). The Epic 1 / Epic 8 suites pin
the behaviour of both families; what they do not pin is the split's
surface contract:

- executor's namespace is the historical import/patch surface, so
  every moved name must remain reachable through it as the SAME
  object the new module defines (``executor.__all__`` pins the full
  pre-split surface), and
- ``get_persisted_run_config`` resolves ``compile_graph`` and
  ``DEFAULT_CHECKPOINT_DB`` through the executor module at call
  time, so patches placed on the executor namespace (the pattern
  test_wave2_executor_bounds / test_epic3_mcp_completion use) keep
  landing in the moved function.
"""
from types import SimpleNamespace

import pytest

from aether_engine.langgraph import executor, fast_chat, manifest, run_config

MANIFEST_NAMES = [
    "_WRITE_TOOL_NAMES",
    "_advance_manifest_to_checkpoint",
    "_new_manifest_tracker",
    "_note_manifest_file_write",
    "_record_manifest_point",
]

RUN_CONFIG_NAMES = [
    "_missing_store_turns",
    "_run_config_snapshot",
    "_turn_key",
    "_turn_text",
    "get_persisted_run_config",
]


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_manifest_names_reexported_identically(name):
    assert getattr(executor, name) is getattr(manifest, name)


@pytest.mark.parametrize("name", RUN_CONFIG_NAMES)
def test_run_config_names_reexported_identically(name):
    assert getattr(executor, name) is getattr(run_config, name)


def test_executor_all_matches_live_namespace():
    live = {name for name in dir(executor) if not name.startswith("__")}
    assert set(executor.__all__) == live


def test_fast_chat_retriable_still_resolves_through_executor():
    # fast_chat imports _is_retriable_error from executor (documented
    # lazy-import cycle); the split must not disturb that surface.
    assert fast_chat._is_retriable_error is executor._is_retriable_error


def _fake_graph(values):
    def aget_state(_config):
        async def coro():
            return SimpleNamespace(values=values)

        return coro()

    return SimpleNamespace(aget_state=aget_state)


async def test_persisted_run_config_honours_executor_patches(
        tmp_path, monkeypatch):
    db = tmp_path / "checkpoints.db"
    db.write_bytes(b"")
    graph = _fake_graph({"run_config": {"provider_name": "patched"}})
    monkeypatch.setattr(executor, "DEFAULT_CHECKPOINT_DB", db)
    monkeypatch.setattr(
        executor, "compile_graph", lambda checkpointer=None: graph)
    result = await executor.get_persisted_run_config("thread-1")
    assert result == {"provider_name": "patched"}


async def test_persisted_run_config_missing_db_via_executor_patch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor, "DEFAULT_CHECKPOINT_DB", tmp_path / "absent.db")
    assert await executor.get_persisted_run_config("thread-1") is None


async def test_persisted_run_config_compile_failure_via_executor_patch(
        tmp_path, monkeypatch):
    db = tmp_path / "checkpoints.db"
    db.write_bytes(b"")
    def exploding_compile(checkpointer=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor, "DEFAULT_CHECKPOINT_DB", db)
    monkeypatch.setattr(executor, "compile_graph", exploding_compile)
    assert await executor.get_persisted_run_config("thread-1") is None
