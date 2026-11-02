"""Engine-tail minors batch — E7-F10, E7-F11, E10-F07, E10-F08,
E10-F09, and the programme-added skills.lock.json backup include.

Six findings (five from the Astra full-product review, one
programme-added during the E10-F03/F04 landing), each re-verified
against current code before fixing:

* **E7-F10** — a valid SKILL.md wins discovery over its adjacent
  manifest.json, but ``SkillManifest.from_skill_md`` never sets
  ``trigger.keywords`` and discovery never consults the shadowed
  JSON, so Markdown skills score 0 in ``match_skills`` and can
  never use task-start auto-match — even when the skill's keywords
  are declared right there (frontmatter or the adjacent manifest).
* **E7-F11** — ``SLASH_COMMAND_LIST`` iterates the manager's
  management listing (``list_all()``, disabled included) without
  filtering, so the wire still advertises a disabled skill's
  command even though the resolver and content loader refuse it.
* **E10-F07** — ``build_diagnostics`` calls ``model_dump(mode=
  "json")`` on the dataclass ``UserConfig``, which has ``to_dict``
  and no ``model_dump``; the AttributeError is swallowed and the
  bundle ships ``{"error": "config could not be serialised"}``
  instead of the promised redacted config.
* **E10-F08** — the app lifespan installs the crash hooks and then
  calls ``engine_state.initialize()`` with no crash-log boundary.
  Uvicorn catches lifespan failures itself, so the hooks never run
  and a caught startup failure never reaches ~/.aether/logs/
  crash.log — the promised lifespan crash banner is missing.
* **E10-F09** — VERSION_CHECK's ``_parse`` strips nondigits from
  each dot-separated piece, so build metadata and prereleases
  become extra numeric parts: ``0.3.5+build.2`` and ``0.3.5-rc.1``
  both report as newer than ``0.3.5``, and an invalid tag becomes
  a successful "no update" result. SemVer precedence says build
  metadata is ignored, a prerelease precedes its release, and an
  unparseable tag is a failed check.
* **P-3** — the backup collector's named include set carries the
  ``skills/`` contents but not ``skills.lock.json`` (the E7-F09
  user-scope lockfile at the data-home root), so a restore keeps
  the skill files but loses the pins that make them reproducible.

TDD: every test here fails on the pre-fix code for the stated
reason and passes after the fix. Socket tests follow the E6-F05
isolated-settings fixture pattern (tmp ``CONFIG_PATH``, audit log
path set before ``initialize_auth()``).
"""
import asyncio
import json
import sys
import threading
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient

import aether_engine.config as config_module
import aether_engine.diagnostics as diagnostics_module
import aether_engine.skills.manager as skills_manager_module
import aether_engine.state as state_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state, lifespan
from aether_engine.backup import create_backup, restore_backup
from aether_engine.config import UserConfig, save_config
from aether_engine.diagnostics import build_diagnostics
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.skills.manager import SkillManager
from aether_engine.version import APP_VERSION

# ---------------------------------------------------------------------------
# E7-F10 — Markdown skills gain matchable keywords
# ---------------------------------------------------------------------------


def _write_md_skill(global_dir, name, frontmatter_extra="", body="MD BODY"):
    skill_dir = global_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n"
        f"{frontmatter_extra}---\n\n{body}\n")
    return skill_dir


def test_markdown_skill_inherits_adjacent_manifest_keywords(tmp_path):
    # The audit's exact shape: a valid SKILL.md plus an adjacent
    # keyword-bearing manifest.json. The body loads from the
    # Markdown file; the keywords must survive discovery too.
    gdir = tmp_path / "skills"
    skill_dir = _write_md_skill(gdir, "mdadjacent")
    (skill_dir / "manifest.json").write_text(json.dumps({
        "name": "mdadjacent",
        "description": "mdadjacent description",
        "trigger": {
            "keywords": ["deploy", "release"],
            "slash_commands": ["/mdadjacent"],
            "auto_match": True,
        },
    }))
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert "MD BODY" in mgr.load_skill_content("mdadjacent")
    assert mgr._index["mdadjacent"].trigger.keywords == ["deploy", "release"]
    assert [m.name for m in mgr.match_skills(
        "please deploy and release", "supervisor")] == ["mdadjacent"]


def test_markdown_skill_frontmatter_keywords(tmp_path):
    # Keywords declared in the SKILL.md frontmatter itself (a
    # top-level YAML list) are the skill's declared metadata too.
    gdir = tmp_path / "skills"
    _write_md_skill(
        gdir, "mdfront",
        frontmatter_extra="keywords:\n  - deploy\n  - release\n")
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert mgr._index["mdfront"].trigger.keywords == ["deploy", "release"]
    assert [m.name for m in mgr.match_skills(
        "deploy then release", "supervisor")] == ["mdfront"]


def test_keywordless_markdown_skill_still_scores_zero(tmp_path):
    # Control: a Markdown skill that declares no keywords anywhere
    # keeps today's behaviour — no invented matches.
    gdir = tmp_path / "skills"
    _write_md_skill(gdir, "mdplain")
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    assert mgr._index["mdplain"].trigger.keywords == []
    assert mgr.match_skills("deploy and release", "supervisor") == []
    assert "MD BODY" in mgr.load_skill_content("mdplain")


# ---------------------------------------------------------------------------
# Shared socket fixture (the E6-F05 isolated-settings pattern)
# ---------------------------------------------------------------------------


class _DummyProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def socket_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=_DummyProtector())
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    save_config(UserConfig(), config_path)

    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry([]))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    yield {"tmp_path": tmp_path, "config_path": config_path}


class _Socket:
    def __enter__(self):
        self._client = TestClient(app)
        self._client.__enter__()
        self._cm = self._client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        self.ws = self._cm.__enter__()
        hello = Event.from_json(self.ws.receive_text())
        assert hello.type == EventType.HELLO
        return self.ws

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._client.__exit__(*exc)
        return False


def _request(ws, event_type, response_type, payload=None):
    ws.send_text(Event(type=event_type, payload=payload or {}).to_json())
    for _ in range(20):
        ev = Event.from_json(ws.receive_text())
        if ev.type == response_type:
            return ev
    raise AssertionError(f"no {response_type} received")


# ---------------------------------------------------------------------------
# E7-F11 — the slash-command list hides disabled skills
# ---------------------------------------------------------------------------


def test_slash_command_list_excludes_disabled_skills(socket_env, monkeypatch):
    gdir = socket_env["tmp_path"] / "skills"
    _write_md_skill(gdir, "tail-on")
    _write_md_skill(gdir, "tail-off")
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    mgr.set_disabled("tail-off", True)
    monkeypatch.setattr(skills_manager_module, "GLOBAL_SKILL_MANAGER", mgr)

    with _Socket() as ws:
        ev = _request(ws, EventType.SLASH_COMMAND_LIST_REQUEST,
                      EventType.SLASH_COMMAND_LIST_RESPONSE)
        commands = {c["command"] for c in ev.payload["commands"]}
        assert "/tail-on" in commands
        assert "/tail-off" not in commands
        # Control: the management listing is NOT the fix site —
        # Settings must still see the disabled skill to re-enable it.
        ev = _request(ws, EventType.SKILL_LIST_REQUEST,
                      EventType.SKILL_LIST_RESPONSE)
        listed = {s["name"]: s for s in ev.payload["skills"]}
        assert listed["tail-off"]["enabled"] is False
        assert listed["tail-on"]["enabled"] is True


# ---------------------------------------------------------------------------
# E10-F07 — diagnostics serialises the real config
# ---------------------------------------------------------------------------


def test_diagnostics_carries_the_real_config(tmp_path):
    audit = tmp_path / "audit.log"
    audit.write_text('{"type": "ENGINE_EVENT", "action": "X"}\n')
    cfg = UserConfig()
    cfg.workspace_roots = ["/SYNTHETIC_CONFIG_ROOT"]
    bundle = build_diagnostics(
        user_config=cfg, health={}, kg_count=1, episodic_count=2,
        audit_log_path=audit, providers=[])
    assert bundle["config"]["workspace_roots"] == ["/SYNTHETIC_CONFIG_ROOT"]
    assert bundle["config"]["default_provider"] == cfg.default_provider
    assert "could not be serialised" not in json.dumps(bundle["config"])


def test_diagnostics_serialisation_failure_surfaces(tmp_path):
    class _Boom:
        def to_dict(self):
            raise RuntimeError("synthetic serialisation failure")

    audit = tmp_path / "audit.log"
    audit.write_text("")
    with pytest.raises(RuntimeError, match="synthetic serialisation failure"):
        build_diagnostics(
            user_config=_Boom(), health={}, kg_count=0, episodic_count=0,
            audit_log_path=audit, providers=[])


# ---------------------------------------------------------------------------
# E10-F08 — caught lifespan startup failures reach the crash log
# ---------------------------------------------------------------------------


def test_lifespan_startup_failure_reaches_crash_log(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics_module, "data_home", lambda: tmp_path)

    def _boom():
        raise OSError("SYNTHETIC_LIFESPAN_FAILURE")

    monkeypatch.setattr(engine_state, "initialize", _boom)
    prev_hook, prev_thread_hook = sys.excepthook, threading.excepthook
    try:
        gen = lifespan(app)
        with pytest.raises(OSError, match="SYNTHETIC_LIFESPAN_FAILURE"):
            asyncio.run(gen.__anext__())
    finally:
        sys.excepthook = prev_hook
        threading.excepthook = prev_thread_hook
    crash = tmp_path / "logs" / "crash.log"
    assert crash.exists()
    text = crash.read_text(encoding="utf-8")
    assert "LIFESPAN" in text
    assert "SYNTHETIC_LIFESPAN_FAILURE" in text


# ---------------------------------------------------------------------------
# E10-F09 — VERSION_CHECK applies SemVer precedence
# ---------------------------------------------------------------------------


def _parts(version):
    return [int(p) for p in version.split(".")]


_CUR = _parts(APP_VERSION)
_SAME = APP_VERSION
_NEWER = f"{_CUR[0]}.{_CUR[1]}.{_CUR[2] + 1}"
_BUILD = f"{APP_VERSION}+build.2"
_PRERELEASE = f"{APP_VERSION}-rc.1"
_NEWER_PRERELEASE = f"{_NEWER}-rc.1"


class _FakeResp:
    def __init__(self, tag):
        self.status_code = 200
        self._tag = tag

    def json(self):
        return {"tag_name": self._tag}


def _fake_client_class(tag):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _FakeResp(tag)

    return _Client


@pytest.mark.parametrize("tag,expected_success,expected_update", [
    (_NEWER, True, True),            # control: genuinely newer release
    (_SAME, True, False),            # control: same version
    (_BUILD, True, False),           # build metadata never affects precedence
    (_PRERELEASE, True, False),      # a prerelease precedes its release
    (_NEWER_PRERELEASE, True, True),  # newer core wins even as a prerelease
    ("nightly", False, None),        # invalid tag: failed check, not "no update"
])
def test_version_check_semver_precedence(
        socket_env, monkeypatch, tag, expected_success, expected_update):
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_class(f"v{tag}"))
    with _Socket() as ws:
        ev = _request(ws, EventType.VERSION_CHECK_REQUEST,
                      EventType.VERSION_CHECK_RESPONSE)
    assert ev.payload["success"] is expected_success
    if expected_update is not None:
        assert ev.payload["update_available"] is expected_update


# ---------------------------------------------------------------------------
# P-3 — the backup collector carries skills.lock.json
# ---------------------------------------------------------------------------


def _manifest_paths(zpath):
    with zipfile.ZipFile(zpath) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    return {f["path"] for f in manifest["files"]}


def test_backup_carries_skills_lockfile_round_trip(tmp_path):
    home = tmp_path / "src_home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    pins = {"skills": [{"name": "pinned-skill",
                        "source": "local",
                        "computedHash": "SYNTHETIC_PIN_HASH"}]}
    (home / "skills.lock.json").write_text(json.dumps(pins, indent=2))
    skills_dir = home / "skills" / "pinned-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: pinned-skill\ndescription: pinned\n---\n\nBody\n")

    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=home)
    assert "skills.lock.json" in _manifest_paths(zpath)
    with zipfile.ZipFile(zpath) as zf:
        assert zf.read("skills.lock.json") == (home / "skills.lock.json").read_bytes()

    dst = tmp_path / "dst_home"
    result = restore_backup(zpath, home=dst)
    assert "skills.lock.json" in result["restored"]
    assert json.loads((dst / "skills.lock.json").read_text()) == pins
