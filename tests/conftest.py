import os
import tempfile
from pathlib import Path

import pytest

# Ensure Qt uses the offscreen platform during headless / test execution
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_engine_config():
    """Never let tests read/write the real ~/.aether/config.json.

    The engine lifespan calls load_config()/save_config(), so without this the
    test suite would pollute the developer's live provider configuration.
    """
    import aether_engine.config as config_module

    original = getattr(config_module, "CONFIG_PATH", None)
    tmp = Path(tempfile.mkdtemp(prefix="aether_test_cfg_")) / "config.json"
    config_module.CONFIG_PATH = tmp
    try:
        yield
    finally:
        if original is not None:
            config_module.CONFIG_PATH = original
