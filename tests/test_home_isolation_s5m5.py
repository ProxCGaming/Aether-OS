"""S5-M5 proof: the whole engine home is sandboxed for the test session.

Before this fix, tests/conftest.py redirected only
``aether_engine.config.CONFIG_PATH``; every other ``~/.aether`` path the
engine resolves (memory DBs, secrets, vault, caches, ...) still pointed at
the developer's real home, so the suite could read and write live state.

conftest.py now redirects ``HOME`` (plus the Windows home variables) to a
fresh temp directory at conftest import time — before any test module
imports the engine — so both the engine's import-time path constants and
its call-time ``Path.home()`` lookups land in the sandbox. These tests
assert that, for a representative spread of both kinds of resolution.
If any of them fail, the isolation has regressed and the suite is
touching real home state again.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def sandbox_home() -> Path:
    raw = os.environ.get("AETHER_TEST_SANDBOX_HOME")
    assert raw, "conftest must expose AETHER_TEST_SANDBOX_HOME for the sandbox"
    return Path(raw)


def _assert_inside_sandbox(label: str, path: Path, sandbox_home: Path) -> None:
    path = Path(path)
    assert path.is_relative_to(sandbox_home), (
        f"{label} resolved outside the test sandbox: {path} "
        f"(sandbox: {sandbox_home}) — the suite would touch real home state"
    )
    assert path.is_relative_to(sandbox_home / ".aether"), (
        f"{label} is in the sandbox but not under its .aether dir: {path}"
    )


def test_home_itself_is_the_sandbox(sandbox_home: Path):
    assert sandbox_home.name.startswith("aether_test_home_")
    assert Path.home() == sandbox_home
    assert os.environ["HOME"] == str(sandbox_home)


def test_import_time_path_constants_are_sandboxed(sandbox_home: Path):
    """Module-level ``Path.home() / ".aether" / ...`` constants, frozen at
    engine import time, must have frozen to the sandbox — this is the half
    of the isolation that only works because conftest sets HOME before any
    test module imports the engine."""
    import aether_engine.artifacts.manager as artifacts_manager
    import aether_engine.artifacts.store as artifacts_store
    import aether_engine.config as config_module
    import aether_engine.cost.ledger as cost_ledger
    import aether_engine.mcp.registry as mcp_registry
    import aether_engine.media.store as media_store
    import aether_engine.rag.store as rag_store
    from aether_engine.langgraph import checkpointer
    from aether_engine.memory import episodic, knowledge_graph, session_store
    from aether_engine.memory.backends import langmem_backend
    from aether_engine.models import reachability
    from aether_engine.scheduler import capability_jobs

    constants = {
        # The autouse conftest fixture rebinds CONFIG_PATH per test to a
        # fresh file inside the sandbox — still inside, by design.
        "config.CONFIG_PATH": config_module.CONFIG_PATH,
        "artifacts.store._DEFAULT_DB_PATH": artifacts_store._DEFAULT_DB_PATH,
        "artifacts.manager._DEFAULT_ARTIFACTS_DIR": (
            artifacts_manager._DEFAULT_ARTIFACTS_DIR
        ),
        "cost.ledger._DEFAULT_DB_PATH": cost_ledger._DEFAULT_DB_PATH,
        "checkpointer.DEFAULT_CHECKPOINT_DB": checkpointer.DEFAULT_CHECKPOINT_DB,
        "mcp.registry._MCP_CONFIG_PATH": mcp_registry._MCP_CONFIG_PATH,
        "media.store._DEFAULT_DB_PATH": media_store._DEFAULT_DB_PATH,
        "langmem_backend._DEFAULT_PATH": langmem_backend._DEFAULT_PATH,
        "episodic.DB_PATH": episodic.DB_PATH,
        "episodic.EMBEDDINGS_DB_PATH": episodic.EMBEDDINGS_DB_PATH,
        "knowledge_graph.DB_PATH": knowledge_graph.DB_PATH,
        "session_store.DB_PATH": session_store.DB_PATH,
        "reachability.DEFAULT_REACHABILITY_DB": (
            reachability.DEFAULT_REACHABILITY_DB
        ),
        "rag.store._DEFAULT_DB_PATH": rag_store._DEFAULT_DB_PATH,
        "capability_jobs.DEFAULT_DB_PATH": capability_jobs.DEFAULT_DB_PATH,
    }
    for label, path in constants.items():
        _assert_inside_sandbox(label, path, sandbox_home)


def test_call_time_path_lookups_are_sandboxed(sandbox_home: Path):
    """Functions and default-constructed stores that call ``Path.home()``
    at runtime must land in the sandbox too."""
    import aether_engine.browser.session as browser_session
    from aether_engine import backup
    from aether_engine.secrets.storage import SecretStore
    from aether_engine.session_export import exporter, importer
    from aether_engine.skills.manager import SkillManager
    from aether_engine.tools import approval_cache, policy
    from aether_engine.vault.store import CredentialVault

    _assert_inside_sandbox("backup.data_home()", backup.data_home(), sandbox_home)
    _assert_inside_sandbox(
        "exporter.data_home()", exporter.data_home(), sandbox_home
    )
    _assert_inside_sandbox(
        "importer.data_home()", importer.data_home(), sandbox_home
    )
    _assert_inside_sandbox(
        "browser dedicated_profile_dir()",
        browser_session.dedicated_profile_dir(),
        sandbox_home,
    )
    _assert_inside_sandbox(
        "approval_cache.default_cache_path()",
        approval_cache.default_cache_path(),
        sandbox_home,
    )
    for prefix in policy._floor_prefixes():
        assert Path(prefix).is_relative_to(sandbox_home), (
            f"policy floor prefix outside the sandbox: {prefix}"
        )
    _assert_inside_sandbox(
        "SkillManager().global_dir", SkillManager().global_dir, sandbox_home
    )
    _assert_inside_sandbox(
        "CredentialVault().db_path", CredentialVault().db_path, sandbox_home
    )
    _assert_inside_sandbox(
        "SecretStore().db_path", SecretStore().db_path, sandbox_home
    )


def test_writes_to_engine_home_stay_in_sandbox(sandbox_home: Path):
    """Behavioural check: saving the engine config writes a real file, and
    that file must appear under the sandbox — nowhere else."""
    import aether_engine.config as config_module
    from aether_engine.config import UserConfig, load_config, save_config

    save_config(UserConfig())
    written = Path(config_module.CONFIG_PATH)
    assert written.exists()
    _assert_inside_sandbox("written config file", written, sandbox_home)
    load_config()  # round-trips against the sandbox file, not a real one
