"""M17 residual — the local-only vision constraint must reach
graph-internal provider construction.

The landed M17 fix (``bfada07``) constrained the websocket's primary
instance and the task-retry factory, but every provider built INSIDE
a run was still unconstrained:

- specialist per-agent overrides (researcher/planner/coder nodes),
- the supervisor's provider resolver,
- the approval-resume instance (websocket) and the resumed run's
  graph config.

So on a local-only vision task, a specialist running a local model
could still have its call retried — image in the messages — on a
cloud fallback inside its own provider instance. These tests pin
the constraint threading: routing decision → executor graph config
→ each construction site, plus the resume path's re-derivation from
the same source the original decision used (the auxiliary vision
override in user config) and the checkpointed history's image parts.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes.coder import coder_node
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.nodes.researcher import researcher_node
from aether_engine.langgraph.nodes.supervisor import _resolve_supervisor_provider
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.memory import session_store
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.policy import local_only_from_vision_override
from aether_engine.tools.registry import ToolRegistry

IMAGE = [{"name": "snip.png", "mime_type": "image/png",
          "data": "data:image/png;base64,AAAA"}]


# ---------------------------------------------------------------------------
# (a) The derivation helper — the resume path's source of truth
# ---------------------------------------------------------------------------

class TestLocalOnlyDerivation:
    def test_local_vision_override_is_local_only(self):
        assert local_only_from_vision_override(
            {"vision": "ollama:::llava"}) is True

    def test_cloud_vision_override_is_not_local_only(self):
        assert local_only_from_vision_override(
            {"vision": "google_gemini:::gemini-3.5-flash"}) is False

    def test_auto_or_absent_override_is_not_local_only(self):
        assert local_only_from_vision_override({"vision": "auto"}) is False
        assert local_only_from_vision_override({}) is False
        assert local_only_from_vision_override(None) is False


# ---------------------------------------------------------------------------
# (b) Graph-internal construction sites honour the run's constraints
# ---------------------------------------------------------------------------

class _StubProvider:
    def __init__(self, label="stub"):
        self.label = label

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        yield StreamChunk(text=f"answer from {self.label}", finish_reason="stop")


@pytest.fixture
def recording_factory(monkeypatch):
    """Replace the provider factory with a recording stub so node
    tests can assert the EXACT construction arguments each site
    passes — the fallback list a real instance would build is
    decided entirely by these kwargs (pinned separately in
    tests/test_epic8_local_vision_fallback.py)."""
    calls = []

    def _factory(p_name, m_name, secret_store, **kwargs):
        calls.append((p_name, m_name, kwargs))
        return _StubProvider(f"{p_name}:{m_name}")

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", _factory)
    return calls


def _node_config(agent, **constraints):
    configurable = {
        "provider": _StubProvider("run"),
        "tool_registry": None,
        "agent_models": {agent: "ollama:llava"},
        "agent_tools": {},
        "secret_store": object(),
        "user_config": UserConfig(),
    }
    configurable.update(constraints)
    return {"configurable": configurable}


def _node_state():
    return {
        "messages": [{"role": "user", "content": "describe this image"}],
        "original_prompt": "describe this image",
        "task_id": "m17-residual",
    }


class TestSpecialistConstructionHonoursConstraints:
    # E5-F02 alignment (a312ce0): every node construction now also
    # receives the run's effective user_config, so the fallback
    # configuration governs graph-internal constructions too. The
    # pins below include that kwarg and assert it is the identical
    # object the node was handed — the passthrough is part of the
    # construction contract now. The local_only / require_vision
    # expectations are unchanged from the M17 originals.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent,node",
        [("researcher", researcher_node),
         ("planner", planner_node),
         ("coder", coder_node)],
    )
    async def test_specialist_override_construction_is_constrained(
            self, recording_factory, agent, node):
        config = _node_config(agent, local_only=True, require_vision=True)
        await node(_node_state(), config)
        assert recording_factory == [
            ("ollama", "llava", {
                "local_only": True, "require_vision": True,
                "user_config": config["configurable"]["user_config"]})
        ]
        assert recording_factory[0][2]["user_config"] is \
            config["configurable"]["user_config"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "agent,node",
        [("researcher", researcher_node),
         ("planner", planner_node),
         ("coder", coder_node)],
    )
    async def test_specialist_override_unconstrained_by_default(
            self, recording_factory, agent, node):
        config = _node_config(agent)
        await node(_node_state(), config)
        assert recording_factory == [
            ("ollama", "llava", {
                "local_only": False, "require_vision": False,
                "user_config": config["configurable"]["user_config"]})
        ]
        assert recording_factory[0][2]["user_config"] is \
            config["configurable"]["user_config"]

    def test_supervisor_override_construction_is_constrained(
            self, recording_factory):
        config = _node_config(
            "supervisor", local_only=True, require_vision=True)
        provider = _resolve_supervisor_provider(config)
        assert isinstance(provider, _StubProvider)
        assert recording_factory == [
            ("ollama", "llava", {
                "local_only": True, "require_vision": True,
                "user_config": config["configurable"]["user_config"]})
        ]
        assert recording_factory[0][2]["user_config"] is \
            config["configurable"]["user_config"]


# ---------------------------------------------------------------------------
# (c) The executor threads the constraints into the graph config —
#     at task start (from kwargs) and on resume (re-derived)
# ---------------------------------------------------------------------------

class _FakeGraph:
    """Captures the config the executor hands the graph."""

    def __init__(self, values=None):
        self.values = values or {}
        self.stream_configs = []

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values, config={})

    async def astream(self, segment_input, config=None, stream_mode=None):
        self.stream_configs.append(config)
        return
        yield  # pragma: no cover — makes this an async generator


@pytest.fixture
def executor_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


class TestExecutorThreadsConstraints:
    @pytest.mark.asyncio
    async def test_run_config_carries_constraints(
            self, executor_env, monkeypatch):
        graph = _FakeGraph()
        monkeypatch.setattr(
            executor_module, "compile_graph", lambda checkpointer=None: graph)
        events = [ev async for ev in executor_module.run_langgraph_task(
            "describe this image",
            _StubProvider("primary"),
            tools=ToolRegistry(),
            task_id="m17-run",
            request_id="req-m17-run",
            session_id="sess-m17-run",
            local_only=True,
            require_vision=True,
        )]
        assert events[-1].type == EventType.TASK_COMPLETED
        assert graph.stream_configs, "graph was never streamed"
        configurable = graph.stream_configs[0]["configurable"]
        assert configurable["local_only"] is True
        assert configurable["require_vision"] is True

    @pytest.mark.asyncio
    async def test_resume_rederives_constraints(
            self, executor_env, monkeypatch):
        image_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
        graph = _FakeGraph(values={
            "task_id": "m17-resume",
            "session_id": "sess-m17-resume",
            "messages": [image_message],
        })
        monkeypatch.setattr(
            executor_module, "compile_graph", lambda checkpointer=None: graph)
        user_config = UserConfig(
            auxiliary_models={"vision": "ollama:::llava"})
        events = [ev async for ev in executor_module.resume_langgraph_task(
            thread_id="sess-m17-resume",
            approved=True,
            provider=_StubProvider("resumed"),
            tools=ToolRegistry(),
            secret_store=object(),
            user_config=user_config,
            session_id="sess-m17-resume",
        )]
        assert graph.stream_configs, "graph was never streamed"
        assert events[-1].type == EventType.TASK_COMPLETED
        configurable = graph.stream_configs[0]["configurable"]
        # local_only re-derived from the same vision override the
        # original routing decision read; require_vision from the
        # checkpointed history actually carrying an image.
        assert configurable["local_only"] is True
        assert configurable["require_vision"] is True

    @pytest.mark.asyncio
    async def test_resume_without_override_or_images_stays_unconstrained(
            self, executor_env, monkeypatch):
        graph = _FakeGraph(values={
            "task_id": "m17-resume-plain",
            "session_id": "sess-m17-resume-plain",
            "messages": [{"role": "user", "content": "plain text"}],
        })
        monkeypatch.setattr(
            executor_module, "compile_graph", lambda checkpointer=None: graph)
        events = [ev async for ev in executor_module.resume_langgraph_task(
            thread_id="sess-m17-resume-plain",
            approved=True,
            provider=_StubProvider("resumed"),
            tools=ToolRegistry(),
            secret_store=object(),
            user_config=UserConfig(),
            session_id="sess-m17-resume-plain",
        )]
        assert events[-1].type == EventType.TASK_COMPLETED
        configurable = graph.stream_configs[0]["configurable"]
        assert configurable["local_only"] is False
        assert configurable["require_vision"] is False


# ---------------------------------------------------------------------------
# (d) Websocket composition: START_TASK forwards the decision's
#     constraints into the graph run, not just the primary instance
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(executor_env, monkeypatch):
    engine_state.token_path = executor_env / "engine.token"
    engine_state.audit_logger.log_path = executor_env / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": executor_env}


def _receive_until_terminal(ws, max_events=20):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED):
            return events
    raise AssertionError(
        f"no terminal event received; got {[ev.type for ev in events]}")


class TestWebsocketForwardsConstraintsToGraph:
    def test_start_task_with_local_vision_forwards_constraints(self, ws_env):
        graph_calls = []

        def fake_run_langgraph_task(*args, **kwargs):
            graph_calls.append(kwargs)

            async def gen():
                yield Event(
                    type=EventType.TASK_CREATED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": "m17-ws", "prompt": kwargs.get("prompt", ""),
                             "state": TaskState.RUNNING.value},
                )
                yield Event(
                    type=EventType.TASK_COMPLETED,
                    request_id=kwargs.get("request_id"),
                    payload={"task_id": "m17-ws", "state": TaskState.SUCCEEDED.value,
                             "response": "ok", "latency_ms": 1.0},
                )

            return gen()

        stack = ExitStack()
        mock_store = stack.enter_context(
            patch.object(ws_module, "SecretStore")).return_value
        mock_store.list_providers.return_value = ["ollama", "google_gemini"]
        mock_store.load_provider.return_value = "fake-key"
        # A bare namespace (no call_stream) makes intent
        # classification fail over to the graph path — the same
        # arrangement as the F-09 pump test.
        stack.enter_context(patch.object(
            ws_module, "_create_provider_instance",
            return_value=SimpleNamespace()))
        stack.enter_context(patch.object(
            ws_module, "run_langgraph_task", fake_run_langgraph_task))

        orig_aux = engine_state.user_config.auxiliary_models
        sid = session_store.create_session("M17 ws")
        try:
            with (
                stack,
                TestClient(app) as client,
                client.websocket_connect(
                    "/ws/tasks",
                    headers={
                        "Authorization": f"Bearer {engine_state.auth_token}",
                    }) as ws,
            ):
                # Set AFTER TestClient entry: the app lifespan
                # re-initializes engine_state (reloading user_config
                # from disk), which would wipe an earlier assignment.
                engine_state.user_config.auxiliary_models = {
                    "vision": "ollama:::llava"}
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.START_TASK,
                    payload={
                        # Work-flavoured wording: intent classification
                        # must route to the graph, not fast chat.
                        "prompt": "Write a script that describes this "
                                  "image and saves a report file",
                        "session_id": sid,
                        "attachments": IMAGE,
                    },
                ).to_json())
                events = _receive_until_terminal(ws)
        finally:
            engine_state.user_config.auxiliary_models = orig_aux

        assert events[-1].type == EventType.TASK_COMPLETED
        assert len(graph_calls) == 1
        assert graph_calls[0].get("local_only") is True
        assert graph_calls[0].get("require_vision") is True
