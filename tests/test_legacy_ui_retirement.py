"""F-16 (EGM C-B11): the retired PySide6 UI must not be a hard dependency.

``aether_ui/`` was retired in favour of the Electron app in ``desktop_app/``
(ADR-0019; ``run_ui.py`` has launched Electron since Phase 10.3 D3). These
tests pin the retirement so it cannot silently regress:

- PySide6 / qasync / markdown — the legacy UI's exclusive dependencies —
  live only in the ``legacy-ui`` optional extra, never in the hard
  ``[project] dependencies`` list.
- No default-path code (engine, shared contracts, worker, launchers)
  imports ``aether_ui``, ``PySide6`` or ``qasync``.
- The engine and the Qt-free parts of ``aether_ui`` still import with
  PySide6 and qasync blocked.
- The retired WS client sends its token in the ``Authorization`` header
  only — never in the URL query string (the pre-retirement behaviour at
  the old ``ws_client.py`` connect path).
"""

import ast
import asyncio
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LEGACY_DEPS = {"pyside6", "qasync", "markdown"}
DEFAULT_PATH_TREES = ["aether_engine", "aether_common", "aether_worker"]
DEFAULT_PATH_FILES = ["run_engine.py", "run_ui.py"]
FORBIDDEN_IMPORT_ROOTS = {"aether_ui", "PySide6", "qasync"}


def _dep_name(spec: str) -> str:
    name = spec.split(";")[0].strip()
    for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "[", " "):
        name = name.split(sep)[0]
    return name.strip().lower().replace("_", "-")


def _project_data() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]


def test_legacy_deps_are_not_hard_dependencies():
    project = _project_data()
    hard = {_dep_name(spec) for spec in project["dependencies"]}
    leaked = LEGACY_DEPS & hard
    assert not leaked, (
        f"legacy-UI dependencies still hard runtime deps: {sorted(leaked)}"
    )


def test_legacy_deps_live_in_the_legacy_ui_extra():
    project = _project_data()
    extras = project.get("optional-dependencies", {})
    assert "legacy-ui" in extras, "pyproject.toml has no legacy-ui extra"
    extra = {_dep_name(spec) for spec in extras["legacy-ui"]}
    missing = LEGACY_DEPS - extra
    assert not missing, (
        f"legacy-UI dependencies missing from the legacy-ui extra: "
        f"{sorted(missing)}"
    )


def _iter_default_path_files():
    for tree in DEFAULT_PATH_TREES:
        yield from sorted((REPO_ROOT / tree).rglob("*.py"))
    for name in DEFAULT_PATH_FILES:
        yield REPO_ROOT / name


def test_no_default_path_imports_of_the_legacy_ui():
    offenders = []
    for path in _iter_default_path_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                        f"imports {root}"
                    )
    assert not offenders, (
        "default-path code imports the retired UI stack:\n"
        + "\n".join(offenders)
    )


def test_engine_imports_with_legacy_ui_stack_blocked():
    script = (
        "import sys\n"
        "for _name in ('PySide6', 'qasync'):\n"
        "    sys.modules[_name] = None\n"  # any import now raises ImportError
        "import aether_engine\n"
        "import aether_ui.state\n"
        "import aether_ui.ws_client\n"
        "assert 'PySide6' not in [m for m in sys.modules if m and "
        "sys.modules[m] is not None and m.startswith('PySide6')]\n"
        "print('blocked-import check OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "engine / Qt-free legacy modules failed to import with PySide6 "
        f"and qasync blocked:\n{result.stdout}\n{result.stderr}"
    )


async def test_ws_client_sends_token_in_header_not_query(monkeypatch):
    import websockets

    from aether_ui.state import UIStateMachine
    from aether_ui.ws_client import AetherWSClient

    captured = {}

    class _FakeWS:
        async def send(self, _data):
            pass

        async def recv(self):
            raise websockets.ConnectionClosed(None, None)

        async def close(self):
            pass

    class _FakeConnection:
        async def __aenter__(self):
            return _FakeWS()

        async def __aexit__(self, *exc):
            return False

    def fake_connect(url, additional_headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = additional_headers or kwargs
        return _FakeConnection()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(
        "aether_ui.ws_client.read_token", lambda _path: "test-token-xyz"
    )

    client = AetherWSClient(UIStateMachine())
    task = client.start_background_listener()
    try:
        for _ in range(100):
            if "url" in captured:
                break
            await asyncio.sleep(0.05)
    finally:
        await client.disconnect()
        # disconnect() cancels the listener task; let it settle without
        # awaiting it (awaiting a cancelled task re-raises CancelledError
        # into this test and would mask the assertions below).
        for _ in range(20):
            if task.done():
                break
            await asyncio.sleep(0.05)

    assert "url" in captured, "WS client never attempted a connection"
    assert "token" not in captured["url"], (
        f"token leaked into the WS URL query string: {captured['url']}"
    )
    assert "test-token-xyz" not in captured["url"]
    headers = captured["headers"]
    assert isinstance(headers, dict), f"no headers captured: {headers!r}"
    assert headers.get("Authorization") == "Bearer test-token-xyz"
