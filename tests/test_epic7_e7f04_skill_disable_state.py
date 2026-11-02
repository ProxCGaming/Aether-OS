"""E7-F04: interrupted skill-disable persistence loses prior disable
choices and leaves live state contradictory.

ADR 0035 / E7-04: disabled skill names must persist and disappear from
every agent-facing path. Pre-fix the seam was threefold:

- ``SkillManager.set_disabled`` mutated the in-memory ``_disabled``
  set BEFORE a direct non-atomic ``write_text`` of ``disabled.json``,
  and purged/rebuilt the live index only AFTER the write succeeded —
  so a failed write left live summaries claiming the skill disabled
  while its slash command and body still worked;
- the truncated file a partial write left behind was silently
  reloaded by a fresh manager as "nothing disabled" — a previously
  saved restriction (alpha) vanished without a trace;
- the WS SKILL_TOGGLE handler never caught the save failure, so the
  client got NO response at all.

These tests pin the fixed behaviour at the real seams, mirroring the
E6-F07 house discipline (temp file + fsync + ``os.replace``; corrupt
file quarantined under a timestamped name with a loud error; merely
unreadable file left untouched):

- an interrupted toggle write leaves the previous disable-state file
  byte-identical, the live manager fully consistent with the on-disk
  state (summaries, slash resolution, body serving), and a fresh
  manager in agreement — the failed toggle is a non-event;
- a corrupt disable-state file is quarantined as
  ``disabled.json.corrupt-<timestamp>`` with an ERROR log before the
  empty fallback is used; an unreadable file is never quarantined or
  overwritten;
- the WS toggle handler replies ``{success: False, error}`` on a save
  failure and the same socket completes a successful toggle after;
- controls: successful disable/enable round trips are byte-for-byte
  and behaviour-for-behaviour what they were before.

The failure injection patches ``pathlib.Path.open`` so writes to the
disable-state file (or its ``.tmp`` sibling) flush an 8-byte prefix to
the real file and then raise ENOSPC — the audit's interrupted save,
synthetic, never a filled disk.
"""

from __future__ import annotations

import errno
import json
import logging
import queue
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import aether_engine.skills.manager as manager_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.skills.manager import SkillManager

DISABLED_NAMES = {"disabled.json", "disabled.json.tmp"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_skill(global_dir: Path, name: str, body: str | None = None):
    """Same layout as tests/test_epic7_plugins_skills.py's helper:
    manifest.json (slash/keywords) + SKILL.md (body)."""
    skill_dir = global_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "description": f"{name} description",
        "trigger": {
            "keywords": ["deploy"],
            "slash_commands": [f"/{name}"],
            "auto_match": True,
        },
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n"
        f"{body or (name.upper() + ' BODY')}\n")
    return skill_dir


class _DyingStream:
    """A text stream whose first write flushes an 8-byte prefix to the
    real file and then raises ENOSPC — an interrupted save."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        self._real.write(data[:8])
        self._real.flush()
        raise OSError(errno.ENOSPC, "No space left on device (injected)")

    def writelines(self, lines):
        self.write("".join(lines))

    def flush(self):
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._real.close()
        return False


def _arm_partial_write(monkeypatch, switch):
    """Patch Path.open so writes to the disable-state file / its temp
    sibling die mid-stream while ``switch["armed"]`` is true."""
    real_open = Path.open

    def dying_open(self, mode="r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if (
            switch["armed"]
            and self.name in DISABLED_NAMES
            and any(c in mode for c in "wxa")
        ):
            return _DyingStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", dying_open)


def _manager(tmp_path: Path) -> SkillManager:
    gdir = tmp_path / "skills"
    _make_skill(gdir, "alpha")
    _make_skill(gdir, "beta")
    return SkillManager(global_dir=gdir, workspace_dirs=[])


def _summaries(mgr: SkillManager) -> dict:
    return {s.name: s for s in mgr.list_all()}


# ---------------------------------------------------------------------------
# Interrupted writes — the failed toggle is a non-event
# ---------------------------------------------------------------------------


class TestInterruptedToggle:
    def test_interrupted_disable_preserves_file_live_and_fresh_state(
            self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path)
        # Cache beta's body, as the audit's repro did.
        assert "BETA BODY" in mgr.load_skill_content("beta")

        mgr.set_disabled("alpha", True)
        disabled_file = tmp_path / "skills" / "disabled.json"
        before = disabled_file.read_bytes()
        assert json.loads(before) == ["alpha"]

        switch = {"armed": True}
        _arm_partial_write(monkeypatch, switch)
        with pytest.raises(OSError):
            mgr.set_disabled("beta", True)

        # The stored file is byte-identical — no truncated fragment.
        assert disabled_file.read_bytes() == before
        # No temp debris left behind.
        assert not (tmp_path / "skills" / "disabled.json.tmp").exists()

        # Live state agrees with the disk: beta still fully enabled on
        # every agent-facing path, alpha still disabled everywhere.
        assert mgr.is_enabled("beta") is True
        assert mgr.is_enabled("alpha") is False
        summaries = _summaries(mgr)
        assert summaries["beta"].enabled is True
        assert summaries["alpha"].enabled is False
        assert mgr.resolve_slash_command("/beta") is not None
        assert "BETA BODY" in mgr.load_skill_content("beta")
        assert mgr.resolve_slash_command("/alpha") is None
        assert mgr.load_skill_content("alpha") == ""

        # A fresh manager agrees — alpha's saved restriction survives.
        fresh = SkillManager(global_dir=tmp_path / "skills",
                             workspace_dirs=[])
        assert fresh.is_enabled("alpha") is False
        assert fresh.is_enabled("beta") is True
        assert fresh.resolve_slash_command("/alpha") is None
        assert "BETA BODY" in fresh.load_skill_content("beta")

    def test_interrupted_enable_preserves_disabled_state(
            self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path)
        mgr.set_disabled("alpha", True)
        disabled_file = tmp_path / "skills" / "disabled.json"
        before = disabled_file.read_bytes()

        switch = {"armed": True}
        _arm_partial_write(monkeypatch, switch)
        with pytest.raises(OSError):
            mgr.set_disabled("alpha", False)

        assert disabled_file.read_bytes() == before
        assert mgr.is_enabled("alpha") is False
        assert _summaries(mgr)["alpha"].enabled is False
        assert mgr.resolve_slash_command("/alpha") is None
        fresh = SkillManager(global_dir=tmp_path / "skills",
                             workspace_dirs=[])
        assert fresh.is_enabled("alpha") is False

    def test_toggle_succeeds_after_an_interrupted_attempt(
            self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path)
        mgr.set_disabled("alpha", True)
        switch = {"armed": True}
        _arm_partial_write(monkeypatch, switch)
        with pytest.raises(OSError):
            mgr.set_disabled("beta", True)
        switch["armed"] = False

        mgr.set_disabled("beta", True)
        disabled_file = tmp_path / "skills" / "disabled.json"
        assert json.loads(disabled_file.read_text()) == ["alpha", "beta"]
        assert mgr.resolve_slash_command("/beta") is None
        fresh = SkillManager(global_dir=tmp_path / "skills",
                             workspace_dirs=[])
        assert fresh.is_enabled("beta") is False


# ---------------------------------------------------------------------------
# Corrupt / unreadable disable-state files at load
# ---------------------------------------------------------------------------


class TestCorruptDisableStateLoad:
    def test_corrupt_file_is_quarantined_with_loud_error(
            self, tmp_path, caplog):
        gdir = tmp_path / "skills"
        _make_skill(gdir, "alpha")
        disabled_file = gdir / "disabled.json"
        disabled_file.write_text('["alpha",', encoding="utf-8")

        with caplog.at_level(logging.ERROR,
                             logger="aether_engine.skills.manager"):
            mgr = SkillManager(global_dir=gdir, workspace_dirs=[])

        # Evidence preserved under a timestamped quarantine name; the
        # live path is clear so the next save starts clean.
        quarantined = list(gdir.glob("disabled.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == '["alpha",'
        assert not disabled_file.exists()
        assert any("corrupt" in r.message.lower()
                   for r in caplog.records if r.levelno >= logging.ERROR)
        # Fallback for this session: nothing disabled.
        assert mgr.is_enabled("alpha") is True

        # A fresh manager now loads cleanly — no repeat quarantine.
        caplog.clear()
        with caplog.at_level(logging.ERROR,
                             logger="aether_engine.skills.manager"):
            SkillManager(global_dir=gdir, workspace_dirs=[])
        assert len(list(gdir.glob("disabled.json.corrupt-*"))) == 1
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_wrong_shape_json_is_treated_as_corrupt(self, tmp_path):
        gdir = tmp_path / "skills"
        _make_skill(gdir, "alpha")
        (gdir / "disabled.json").write_text('{"alpha": true}',
                                            encoding="utf-8")
        mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
        assert len(list(gdir.glob("disabled.json.corrupt-*"))) == 1
        assert mgr.is_enabled("alpha") is True

    def test_unreadable_file_is_left_untouched(self, tmp_path, monkeypatch,
                                               caplog):
        gdir = tmp_path / "skills"
        _make_skill(gdir, "alpha")
        disabled_file = gdir / "disabled.json"
        disabled_file.write_text('["alpha"]', encoding="utf-8")

        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text

        def denied_bytes(self, *args, **kwargs):
            if self.name == "disabled.json":
                raise PermissionError("Permission denied (injected)")
            return real_read_bytes(self, *args, **kwargs)

        def denied_text(self, *args, **kwargs):
            if self.name == "disabled.json":
                raise PermissionError("Permission denied (injected)")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", denied_bytes)
        monkeypatch.setattr(Path, "read_text", denied_text)
        with caplog.at_level(logging.ERROR,
                             logger="aether_engine.skills.manager"):
            mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
        monkeypatch.undo()

        # Never quarantined, never overwritten — evidence untouched;
        # the session falls back to nothing-disabled with a loud log.
        assert disabled_file.read_text(encoding="utf-8") == '["alpha"]'
        assert list(gdir.glob("disabled.json.corrupt-*")) == []
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        assert mgr.is_enabled("alpha") is True

    def test_missing_file_loads_clean_without_quarantine(self, tmp_path):
        gdir = tmp_path / "skills"
        _make_skill(gdir, "alpha")
        mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
        assert mgr.is_enabled("alpha") is True
        assert list(gdir.glob("disabled.json*")) == []


# ---------------------------------------------------------------------------
# Controls — successful toggles unchanged
# ---------------------------------------------------------------------------


class TestSuccessfulToggleControls:
    def test_round_trip_bytes_and_behaviour_unchanged(self, tmp_path):
        mgr = _manager(tmp_path)
        disabled_file = tmp_path / "skills" / "disabled.json"

        mgr.set_disabled("beta", True)
        # Same on-disk format as before the fix: sorted JSON list.
        assert disabled_file.read_text(encoding="utf-8") == \
            json.dumps(["beta"], indent=2)
        assert _summaries(mgr)["beta"].enabled is False
        assert mgr.resolve_slash_command("/beta") is None
        assert mgr.load_skill_content("beta") == ""
        # No temp or quarantine debris from a successful save.
        assert list((tmp_path / "skills").glob("disabled.json.*")) == []

        mgr.set_disabled("beta", False)
        assert disabled_file.read_text(encoding="utf-8") == \
            json.dumps([], indent=2)
        assert _summaries(mgr)["beta"].enabled is True
        assert mgr.resolve_slash_command("/beta") is not None
        assert "BETA BODY" in mgr.load_skill_content("beta")


# ---------------------------------------------------------------------------
# WS seam — failure replies, session survives
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws_env(tmp_path, monkeypatch):
    # Fixture law: the audit logger's path must be repointed into tmp
    # BEFORE initialize_auth().
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        "aether_engine.memory.session_store.DB_PATH",
        tmp_path / "test_memory.db")

    gdir = tmp_path / "skills"
    _make_skill(gdir, "alpha")
    _make_skill(gdir, "beta")
    mgr = SkillManager(global_dir=gdir, workspace_dirs=[])
    monkeypatch.setattr(manager_module, "GLOBAL_SKILL_MANAGER", mgr)
    yield mgr


def _recv(ws, timeout=30.0) -> Event:
    """Receive one Event, failing (never hanging) if the server is quiet."""
    q: queue.Queue = queue.Queue()

    def _do():
        try:
            q.put(("ok", ws.receive_text()))
        except Exception as e:  # noqa: BLE001 — relayed to the test thread
            q.put(("err", e))

    threading.Thread(target=_do, daemon=True).start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(
            f"No WebSocket message within {timeout}s — the session was "
            "probably dropped by an unhandled handler error."
        ) from None
    if kind == "err":
        raise value
    return Event.from_json(value)


def _send(ws, type_, payload=None, request_id="r1") -> Event:
    ws.send_text(Event(
        type=type_, request_id=request_id, payload=payload or {}
    ).to_json())
    return _recv(ws)


def _connect_ws(client):
    # The app lifespan regenerates engine_state.auth_token — read fresh.
    return client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    )


def test_ws_toggle_failure_replies_and_session_survives(
        ws_env, tmp_path, monkeypatch):
    mgr = ws_env
    disabled_file = tmp_path / "skills" / "disabled.json"
    switch = {"armed": False}
    _arm_partial_write(monkeypatch, switch)

    with TestClient(app) as client, _connect_ws(client) as ws:
        hello = _recv(ws)
        assert hello.type == EventType.HELLO

        # Control: a normal disable succeeds and replies.
        resp = _send(ws, EventType.SKILL_TOGGLE_REQUEST,
                     {"name": "alpha", "enabled": False}, "t1")
        assert resp.type == EventType.SKILL_TOGGLE_RESPONSE
        assert resp.payload["success"] is True
        before = disabled_file.read_bytes()
        assert json.loads(before) == ["alpha"]

        # Interrupted save: the handler must reply, not drop.
        switch["armed"] = True
        resp = _send(ws, EventType.SKILL_TOGGLE_REQUEST,
                     {"name": "beta", "enabled": False}, "t2")
        assert resp.type == EventType.SKILL_TOGGLE_RESPONSE
        assert resp.payload["success"] is False
        assert resp.payload.get("error")
        assert resp.payload["name"] == "beta"
        assert resp.payload["enabled"] is True  # beta is still enabled
        switch["armed"] = False

        # Stored state untouched; live manager consistent.
        assert disabled_file.read_bytes() == before
        assert mgr.is_enabled("beta") is True
        assert mgr.resolve_slash_command("/beta") is not None

        # The SAME socket completes a successful toggle afterwards.
        resp = _send(ws, EventType.SKILL_TOGGLE_REQUEST,
                     {"name": "beta", "enabled": False}, "t3")
        assert resp.payload["success"] is True
        assert resp.payload["enabled"] is False
        assert json.loads(disabled_file.read_text()) == ["alpha", "beta"]
        assert mgr.resolve_slash_command("/beta") is None
