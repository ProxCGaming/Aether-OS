"""Epic 2 minors batch: E2-F07, E2-F08, E2-F09.

E2-F07: the Tool Settings list must report the policy enforcement
will actually apply (same resolver, real registry + approval mode),
and the policy setter must accept "Always Allow" for safe registered
tools — including filesystem and MCP tools, not just the legacy
factories it used to reconstruct.

E2-F08: search_text must apply its scan cap DURING enumeration (a
lazy walk, not a materialised candidate list) and keep its output
within the tool character budget — a single matching line is clamped
loudly and the total output is capped loudly.

E2-F09: fetch_url's FETCH_TIMEOUT_S is a TOTAL deadline across the
whole fetch, including body streaming — a slow-drip response whose
chunks are each individually timely must still fail at the deadline,
and a transport that returns after the deadline never reports success.
"""

from __future__ import annotations

from types import SimpleNamespace

from aether_engine.tools import filesystem, web_fetch
from aether_engine.tools.filesystem import _search_text_fn
from aether_engine.tools.web_fetch import FetchResponse, _fetch_url_impl

PUBLIC_IP = "93.184.216.34"


# ---------------------------------------------------------------------------
# E2-F07 — Settings list/setter resolve the real policy
# ---------------------------------------------------------------------------


class TestToolPolicySettingsSurface:
    def setup_method(self):
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch

        from aether_engine.app import engine_state
        from aether_engine.config import UserConfig

        self._patch = patch
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = _P(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        engine_state.initialize_auth()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        self._saved_config = engine_state.user_config
        engine_state.user_config = UserConfig()

    def teardown_method(self):
        from aether_engine.app import engine_state

        engine_state.user_config = self._saved_config
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        from aether_engine.app import engine_state

        token = engine_state.token_path.read_text(encoding="utf-8").strip()
        return client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {token}"}
        )

    def _send(self, ws, event_type, payload):
        from aether_common.contracts import Event

        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def _tool_list(self, ws):
        from aether_common.contracts import EventType

        resp = self._send(ws, EventType.TOOL_LIST_REQUEST, {})
        assert resp.type == EventType.TOOL_LIST_RESPONSE
        return {t["function"]["name"]: t for t in resp.payload["tools"]}

    def test_tool_list_matches_enforcement_in_ask_mode(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            tools = self._tool_list(ws)
        # READ_ONLY defaults resolve "allow" in the real resolver; the
        # list used to report "ask" for all of these because it resolved
        # without the registry (risk defaulted to EXECUTE).
        for name in ("read_file", "project_tree", "search_text", "fetch_url"):
            assert tools[name]["effective_policy"] == "allow", name
            assert tools[name]["always_allow_permitted"] is True, name
        # Risky tools stay "ask" by default and cannot be Always Allowed.
        for name in ("write_file", "execute_shell", "delete_file"):
            assert tools[name]["effective_policy"] == "ask", name
            assert tools[name]["always_allow_permitted"] is False, name

    def test_tool_list_reflects_write_approval_mode(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app, engine_state

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            # Set AFTER startup: TestClient entry re-initialises
            # engine_state and reloads user_config from disk.
            engine_state.user_config.approval_mode = "write"
            tools = self._tool_list(ws)
        # E2-08: write mode relaxes the WRITE default to allow — the
        # list must show what enforcement will do under the live mode.
        assert tools["write_file"]["effective_policy"] == "allow"
        # DESTRUCTIVE/EXECUTE are not relaxed by write mode.
        assert tools["delete_file"]["effective_policy"] == "ask"
        assert tools["execute_shell"]["effective_policy"] == "ask"

    def test_policy_set_accepts_always_allow_for_safe_registered_tools(
        self, monkeypatch
    ):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app, engine_state
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
        from aether_engine.tools.registry import Tool, ToolRisk

        fake_mcp = Tool(
            name="mcp_fake_lookup",
            description="synthetic READ_ONLY MCP tool",
            execute_fn=lambda **kw: "ok",
            risk=ToolRisk.READ_ONLY,
            source="mcp:fake",
        )
        monkeypatch.setattr(
            GLOBAL_MCP_REGISTRY,
            "get_mcp_tools",
            lambda risk_overrides=None: [fake_mcp],
        )
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            for name in ("read_file", "project_tree", "search_text", "mcp_fake_lookup"):
                resp = self._send(
                    ws,
                    EventType.TOOL_POLICY_SET_REQUEST,
                    {"tool_name": name, "policy": "Always Allow"},
                )
                assert resp.type == EventType.TOOL_POLICY_SET_RESPONSE
                assert resp.payload["policy"] == "Always Allow", name
                assert engine_state.user_config.tool_policies[name] == "Always Allow", name

    def test_policy_set_still_downgrades_risky_and_unknown_tools(self):
        from starlette.testclient import TestClient

        from aether_common.contracts import EventType
        from aether_engine.app import app, engine_state

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            for name in ("write_file", "execute_shell", "no_such_tool_xyz"):
                resp = self._send(
                    ws,
                    EventType.TOOL_POLICY_SET_REQUEST,
                    {"tool_name": name, "policy": "Always Allow"},
                )
                assert resp.payload["policy"] == "Require Approval", name
                assert (
                    engine_state.user_config.tool_policies[name] == "Require Approval"
                ), name


# ---------------------------------------------------------------------------
# E2-F08 — search_text scan/output budgets
# ---------------------------------------------------------------------------


def test_search_stops_walking_at_scan_cap(tmp_path, monkeypatch):
    folder = tmp_path / "many"
    folder.mkdir()
    for i in range(12):
        (folder / f"f{i:02d}.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(filesystem, "SEARCH_MAX_FILES_SCANNED", 5)

    pulled = 0
    real_walk = filesystem._walk_files

    def counting_walk(base, include_hidden):
        nonlocal pulled
        for p in real_walk(base, include_hidden):
            pulled += 1
            yield p

    monkeypatch.setattr(filesystem, "_walk_files", counting_walk)
    out = _search_text_fn("needle", path=str(folder), workspace_roots=[str(tmp_path)])
    assert "[stopped after scanning 5 files" in out
    # The cap applies during enumeration: the walker is not drained
    # (12 candidates) just to scan 5 — at most the 5 scanned plus the
    # one candidate whose pull trips the cap check.
    assert pulled <= 6, f"walker yielded {pulled} candidates for a 5-file scan cap"


def test_search_clamps_oversized_matching_line(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("needle " + "x" * 100_000 + "\n", encoding="utf-8")
    out = _search_text_fn("needle", path=str(big), workspace_roots=[str(tmp_path)])
    assert "truncated" in out  # the clamp is loud, never silent
    assert len(out) <= 20_000 + 100  # the Tool.max_output_chars budget


def test_search_total_output_stays_within_budget(tmp_path):
    for i in range(60):
        (tmp_path / f"m{i:02d}.txt").write_text(
            "needle " + "y" * 500 + "\n", encoding="utf-8"
        )
    out = _search_text_fn("needle", path=str(tmp_path), workspace_roots=[str(tmp_path)])
    assert "[output capped at" in out
    assert len(out) <= 20_000 + 200


# ---------------------------------------------------------------------------
# E2-F09 — fetch_url total deadline
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 500.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _install_fake_clock(monkeypatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(web_fetch, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


def test_slow_drip_stream_fails_at_total_deadline(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    monkeypatch.setenv(web_fetch.ALLOW_LOCAL_FETCH_ENV, "1")

    class _DripResponse:
        status_code = 200

        def __init__(self):
            self.headers = {"content-type": "text/plain; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            # Every chunk is individually timely (6s < the 15s budget
            # passed as the per-request timeout) but the drip totals
            # 24 simulated seconds — the total deadline must govern.
            for chunk in (b"alpha ", b"beta ", b"gamma ", b"delta"):
                clock.advance(6.0)
                yield chunk

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            return _DripResponse()

    monkeypatch.setattr(web_fetch.httpx, "Client", _FakeClient)
    result = _fetch_url_impl("http://example.com/page")
    assert result.startswith("Error:")
    assert "timed out" in result


def test_transport_returning_after_deadline_never_reports_success(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    monkeypatch.setattr(web_fetch, "_resolve_addresses", lambda host: [PUBLIC_IP])

    def _late_transport(url: str, timeout_s: float) -> FetchResponse:
        clock.advance(web_fetch.FETCH_TIMEOUT_S + 5.0)
        return FetchResponse(
            status=200,
            headers={"content-type": "text/plain; charset=utf-8"},
            body=b"late body",
        )

    monkeypatch.setattr(web_fetch, "_request_once", _late_transport)
    result = _fetch_url_impl("http://example.com/page")
    assert "timed out" in result
    assert "late body" not in result
