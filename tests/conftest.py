import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Ensure Qt uses the offscreen platform during headless / test execution
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ---------------------------------------------------------------------------
# S5-M5: sandbox the WHOLE engine home, not just one config path.
#
# How the engine resolves its home (verified by grep over aether_engine/):
# there is NO AETHER_HOME-style environment override anywhere. Every piece
# of home state — config, memory DBs, secrets, vault, caches, skills,
# browser profile, attachments — is derived from ``Path.home() / ".aether"``
# in exactly two ways:
#
#   1. Module-level constants frozen at import time, e.g.
#      ``config.CONFIG_PATH``, ``memory.episodic.DB_PATH`` /
#      ``EMBEDDINGS_DB_PATH``, ``memory.knowledge_graph.DB_PATH``,
#      ``memory.session_store.DB_PATH``,
#      ``langgraph.checkpointer.DEFAULT_CHECKPOINT_DB``,
#      ``cost.ledger._DEFAULT_DB_PATH``, ``artifacts.store._DEFAULT_DB_PATH``,
#      ``media.store._DEFAULT_DB_PATH``, ``mcp.registry._MCP_CONFIG_PATH``,
#      ``rag.store._DEFAULT_DB_PATH``,
#      ``scheduler.capability_jobs.DEFAULT_DB_PATH``,
#      ``models.reachability.DEFAULT_REACHABILITY_DB``,
#      ``memory.backends.langmem_backend._DEFAULT_PATH``.
#   2. Call-time ``Path.home()`` lookups, e.g. ``backup.data_home()``,
#      ``session_export`` exporter/importer ``data_home()``,
#      ``browser.session.dedicated_profile_dir()``, ``vault.store`` and
#      ``secrets.storage`` default DB paths, ``skills.manager`` global dir,
#      ``state.EngineState``'s PluginInstaller dir,
#      ``tools.approval_cache`` default path, ``tools.policy`` protected
#      prefixes, ``plugins.installer`` skills destinations.
#
# ``Path.home()`` resolves from the ``HOME`` environment variable on POSIX
# (and from ``USERPROFILE`` / ``HOMEDRIVE``+``HOMEPATH`` on Windows), so
# redirecting those variables intercepts BOTH kinds of lookup at the one
# level that governs all of them — including in subprocesses the suite
# spawns, which inherit this environment.
#
# Timing is the reason this lives at conftest IMPORT time instead of in a
# fixture: pytest imports this conftest before it imports any test module,
# and test modules are what import ``aether_engine``. A fixture — even a
# session-scoped autouse one — only runs after collection-time imports, by
# which point the module-level constants in group (1) would already be
# frozen to the developer's real home. Setting the environment here means
# those constants freeze to the sandbox instead.
#
# Tests that need a *different* per-test home (e.g. the plugin installer
# tests) can still monkeypatch ``HOME`` per test; monkeypatch restores to
# this sandbox afterwards, never to the real home.
# ---------------------------------------------------------------------------
_ORIGINAL_HOME = os.environ.get("HOME")
SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="aether_test_home_"))
(SANDBOX_HOME / ".aether").mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(SANDBOX_HOME)
if os.name == "nt":  # Path.home() on Windows reads these, not HOME.
    os.environ["USERPROFILE"] = str(SANDBOX_HOME)
    if SANDBOX_HOME.drive:
        os.environ["HOMEDRIVE"] = SANDBOX_HOME.drive
        os.environ["HOMEPATH"] = str(SANDBOX_HOME)[len(SANDBOX_HOME.drive) :] or "\\"
# Exposed so the isolation proof test (test_home_isolation_s5m5.py) can
# assert the engine resolved its paths inside this exact directory.
os.environ["AETHER_TEST_SANDBOX_HOME"] = str(SANDBOX_HOME)
atexit.register(shutil.rmtree, SANDBOX_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_engine_config():
    """Never let tests read/write the real ~/.aether/config.json.

    The engine lifespan calls load_config()/save_config(), so without this the
    test suite would pollute the developer's live provider configuration.

    Belt-and-braces on top of the import-time HOME sandbox above: this
    rebinds the (already sandboxed) CONFIG_PATH constant to a fresh
    per-test file so config written by one test cannot leak into the next.
    The per-test directory lives inside the sandbox home, so CONFIG_PATH
    stays under the sandbox for the whole suite (the S5-M5 proof test
    asserts exactly that).
    """
    import aether_engine.config as config_module

    original = getattr(config_module, "CONFIG_PATH", None)
    tmp = (
        Path(tempfile.mkdtemp(prefix="cfg_", dir=SANDBOX_HOME / ".aether"))
        / "config.json"
    )
    config_module.CONFIG_PATH = tmp
    try:
        yield
    finally:
        if original is not None:
            config_module.CONFIG_PATH = original
