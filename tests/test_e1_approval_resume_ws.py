"""E1-F03 + E1-F04: approval resume over the websocket.

The parity suite (test_epic1_parity.py) proves the executor-level
resume: pause on a "Require Approval" policy, call
``resume_langgraph_task`` directly, tool executes exactly once. What
it cannot see is the product path — the websocket route — where:

* F03: the approval arrives on a FRESH connection (the client
  reconnected, or the engine restarted; the pending approval is
  re-offered from the checkpointer on connect). The resume branch
  read the connection-local ``session_id`` — only ever assigned by
  START_TASK on that same connection — so the resumed task never
  started and zero frames were sent.
* F04: the same branch rebuilt the run from the CURRENT globals
  (active provider/model, global agent_models/agent_tools, global
  user config and MCP risk overrides) and passed neither
  ``session_id`` nor ``forced_agent`` to the resume, discarding the
  paused task's effective configuration (ADR 0027 D4 requires
  full-config continuity).

These tests pause a real task through the executor (durable
checkpoint, exactly what a disconnect/restart leaves behind), then
drive the approval through a fresh TestClient websocket connection
and assert on the frames, the executed calls, and the recorded
hand-off at the route's seams (provider factory, MCP risk overrides,
resume kwargs, node-level provider construction).
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.routes import websocket as ws_module
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "parity_probe"
TOOL_ARGS = {"q": "x"}
TOOL_RESULT = "probe-result-42"
FINAL_TEXT = "Specialist final answer."


# --------------------------------------------------------------------------
# Fakes (house pattern: scripted providers, recorded seams, no network)
# --------------------------------------------------------------------------

class ScriptedProvider:
    """Graph provider: supervisor delegates to planner; the planner's
    first call requests the probe tool, later calls return final text.

    One instance is shared between the pausing run and the resumed
    run (the route's provider factory returns it), so the planner
    call count persists across the pause exactly as the parity
    suite's ScriptedGraphProvider relies on. ``provider_name`` /
    ``model`` mirror LiteLLMProvider's attributes — the run-config
    snapshot records them.
    """

    def __init__(self, provider_name="taskprov", model="task-model"):
        self.provider_name = provider_name
        self.model = model
        self.calls = []
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name})
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-e1-resume-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "resume routing", "briefing": ""}',
            finish_reason="stop",
        )


def _probe_registry(executed):
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return TOOL_RESULT

    registry.register(Tool(
        name=TOOL_NAME,
        description="E1 resume probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Isolate a websocket test: tmp token/audit/session/checkpoint
    stores, no real memory writes on completion, engine globals saved
    and restored (they are process-wide singletons)."""
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    token = engine_state.initialize_auth()

    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "e1-resume/artifact.txt")

    saved = (engine_state.user_config, engine_state.active_provider,
             engine_state.active_model)
    yield SimpleNamespace(token=token, tmp_path=tmp_path)
    (engine_state.user_config, engine_state.active_provider,
     engine_state.active_model) = saved


def _patch_resume_surface(monkeypatch, provider, registry):
    """Replace the route's construction seams with recording fakes.

    Returns (route_factory_calls, node_factory_calls, mcp_override_calls):
    the provider factory the route uses, the factory graph nodes use
    for per-agent overrides, and the risk overrides the route passes
    to the MCP registry.
    """
    route_factory_calls = []

    def fake_route_factory(name, model, secret_store, **kwargs):
        route_factory_calls.append((name, model))
        return provider

    monkeypatch.setattr(
        ws_module, "_create_provider_instance", fake_route_factory)
    monkeypatch.setattr(
        ws_module, "build_tool_registry", lambda **kwargs: registry)

    node_factory_calls = []

    def fake_node_factory(name, model, secret_store, **kwargs):
        node_factory_calls.append((name, model))
        return provider

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", fake_node_factory)

    mcp_override_calls = []

    def fake_get_mcp_tools(risk_overrides=None, **kwargs):
        mcp_override_calls.append(risk_overrides)
        return []

    monkeypatch.setattr(
        GLOBAL_MCP_REGISTRY, "get_mcp_tools", fake_get_mcp_tools)
    return route_factory_calls, node_factory_calls, mcp_override_calls


# --------------------------------------------------------------------------
# Pause setup (executor-level, the durable state a reconnect finds)
# --------------------------------------------------------------------------

async def _run_to_pause(provider, registry, session_id, user_config, **run_kwargs):
    events = [ev async for ev in executor_module.run_langgraph_task(
        "please run the resume probe",
        provider,
        tools=registry,
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config,
        **run_kwargs,
    )]
    return events


def _make_paused_task(provider, registry, user_config, **run_kwargs):
    session_id = session_store.create_session("E1 resume test")
    events = asyncio.run(
        _run_to_pause(provider, registry, session_id, user_config, **run_kwargs))
    types = [ev.type for ev in events]
    assert EventType.TOOL_APPROVAL_REQUEST in types
    assert EventType.TASK_INTERRUPTED in types
    return session_id


def _drive_approval(session_id):
    """The raw drive: connect, take the recovered approval offer,
    grant it, collect frames until the task settles.

    The token is read live from engine_state (as test_engine_ws.py
    does): the app lifespan re-initialises auth on startup, so a
    token captured before TestClient starts may be stale."""
    frames = []
    error = None
    with TestClient(app) as client:
        token = engine_state.auth_token
        with client.websocket_connect(
                "/ws/tasks",
                headers={"Authorization": f"Bearer {token}"}) as ws:
            hello = Event.from_json(ws.receive_text())
            assert hello.type == EventType.HELLO
            frames.append(hello)
            offered = None
            for _ in range(5):
                ev = Event.from_json(ws.receive_text())
                frames.append(ev)
                if ev.type == EventType.TOOL_APPROVAL_REQUEST:
                    offered = ev
                    break
            assert offered is not None, (
                f"pending approval was not re-offered on connect: {frames}")
            assert offered.payload["approval_key"] == session_id
            ws.send_text(Event(
                type=EventType.TOOL_APPROVAL_GRANTED,
                request_id="approve-req-1",
                payload={"approval_key": session_id},
            ).to_json())
            try:
                for _ in range(40):
                    ev = Event.from_json(ws.receive_text())
                    frames.append(ev)
                    if ev.type in (EventType.TASK_COMPLETED,
                                   EventType.TASK_FAILED):
                        break
            except Exception as exc:  # noqa: BLE001 — a dead socket is the F03 symptom; assertions below judge it
                error = exc
    return frames, error


def _approve_on_fresh_connection(session_id, timeout=120):
    """_drive_approval on a bounded thread.

    Starlette's TestClient receive has no timeout, and the pre-fix
    F03 failure mode is precisely "handler died, socket never
    delivers another frame" — a blocked receive would hang the
    suite instead of failing. The drive therefore runs on a daemon
    thread; if it has not finished within ``timeout`` the caller
    gets the frames so far plus a TimeoutError to assert against.
    Connect-phase failures (auth, no recovered offer) are real test
    errors and are re-raised, not folded into the outcome."""
    outcome = {}

    def _run():
        try:
            frames, error = _drive_approval(session_id)
            outcome["frames"] = frames
            outcome["error"] = error
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread below
            outcome["raise"] = exc

    thread = threading.Thread(
        target=_run, daemon=True, name="e1-resume-ws-drive")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return outcome.get("frames", []), TimeoutError(
            f"no completion frame within {timeout}s — "
            f"the resumed task never started")
    if "raise" in outcome:
        raise outcome["raise"]
    return outcome["frames"], outcome["error"]


# --------------------------------------------------------------------------
# F03: approval on a fresh connection resumes the paused task
# --------------------------------------------------------------------------

def test_approval_on_fresh_connection_resumes_paused_task(ws_env, monkeypatch):
    executed = []
    provider = ScriptedProvider()
    registry = _probe_registry(executed)
    session_id = _make_paused_task(
        provider, registry,
        UserConfig(tool_policies={TOOL_NAME: "Require Approval"}),
    )
    assert executed == [], "tool must not execute before approval"

    engine_state.active_provider = "globalprov"
    engine_state.active_model = "global-model"
    engine_state.user_config = UserConfig()
    route_calls, _, _ = _patch_resume_surface(monkeypatch, provider, registry)

    frames, error = _approve_on_fresh_connection(session_id)

    types = [ev.type for ev in frames]
    assert EventType.TASK_COMPLETED in types, (
        f"resumed task never completed on a fresh connection "
        f"(frames={types}, connection error={error!r})")
    completed = next(ev for ev in frames if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert executed == [TOOL_ARGS], (
        f"approved call must execute exactly once, got {executed}")
    # The resume provider is the TASK's provider, not the global one.
    assert route_calls == [("taskprov", "task-model")]


# --------------------------------------------------------------------------
# F04: resume carries the paused task's effective configuration
# --------------------------------------------------------------------------

def test_resume_uses_paused_tasks_effective_config(ws_env, monkeypatch):
    executed = []
    provider = ScriptedProvider()
    registry = _probe_registry(executed)
    task_root = str(ws_env.tmp_path / "task-root")
    task_config = UserConfig(
        agent_models={"planner": "overlay:planner-model"},
        agent_tools={"planner": [TOOL_NAME]},
        tool_policies={TOOL_NAME: "Require Approval"},
        mcp_tool_risk_overrides={"srv__tool": "read_only"},
    )
    session_id = _make_paused_task(
        provider, registry, task_config,
        agent_models={"planner": "overlay:planner-model"},
        agent_tools={"planner": [TOOL_NAME]},
        forced_agent="planner",
        workspace_roots=[task_root],
    )
    assert executed == []

    # Globals the resume must NOT fall back to.
    engine_state.active_provider = "globalprov"
    engine_state.active_model = "global-model"
    engine_state.user_config = UserConfig(
        agent_models={"planner": "global:global-model"},
        agent_tools={"planner": ["global_tool"]},
        mcp_tool_risk_overrides={"srv__tool": "destructive"},
    )
    route_calls, node_calls, mcp_calls = _patch_resume_surface(
        monkeypatch, provider, registry)

    recorded = {}
    real_resume = ws_module.resume_langgraph_task

    def spy_resume(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(ws_module, "resume_langgraph_task", spy_resume)

    frames, error = _approve_on_fresh_connection(session_id)

    types = [ev.type for ev in frames]
    assert EventType.TASK_COMPLETED in types, (
        f"resumed task never completed (frames={types}, error={error!r})")
    assert executed == [TOOL_ARGS]

    kwargs = recorded["kwargs"]
    assert route_calls == [("taskprov", "task-model")]
    assert mcp_calls == [{"srv__tool": "read_only"}], (
        f"MCP tools rebuilt with {mcp_calls}, not the task's effective "
        f"risk overrides")
    assert kwargs.get("session_id") == session_id
    assert kwargs.get("forced_agent") == "planner"
    assert kwargs.get("agent_models") == {"planner": "overlay:planner-model"}
    assert kwargs.get("agent_tools") == {"planner": [TOOL_NAME]}
    assert kwargs.get("workspace_roots") == [task_root]
    resume_config = kwargs.get("user_config")
    assert isinstance(resume_config, UserConfig)
    assert resume_config.agent_models == {"planner": "overlay:planner-model"}
    assert resume_config.mcp_tool_risk_overrides == {"srv__tool": "read_only"}
    # End-to-end: the resumed planner constructed its provider from the
    # task's per-agent overlay, not the global one.
    assert ("overlay", "planner-model") in node_calls, (
        f"node provider constructions: {node_calls}")
    assert ("global", "global-model") not in node_calls
