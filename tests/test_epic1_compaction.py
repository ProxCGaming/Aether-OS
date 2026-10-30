"""Epic 1, E1-06: conversation compaction / summarisation.

Pins the E1-06 contract with stub providers only (no network):

- The pure core folds the older turns once either threshold is crossed,
  keeps the recent tail verbatim, never starts the tail with an orphan
  tool result, and folds nothing when the marker already covers all
  but the tail.
- Compaction is incremental: a second round hands the summariser the
  existing summary plus ONLY the newly aged turns.
- The model-facing view (specialists + supervisor routing prompt) is
  summary + tail; without a summary it is the full history, unchanged.
- Through the executor: a long thread compacts at run assembly, the
  checkpoint keeps the FULL history (count and content), and a short
  thread behaves exactly as before (no summariser call, no summary).
"""
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.compaction import (
    CompactionConfig,
    compact_history,
    compaction_config_from,
    context_summary_section,
    make_provider_summariser,
    model_facing_messages,
    plan_compaction,
    resolve_summariser_provider,
)
from aether_engine.langgraph.graph import compile_graph
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.state import message_dicts
from aether_engine.langgraph.supervisor_model import supervisor_routing_prompt
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.registry import ToolRegistry

SUMMARY_TEXT = "SUMMARY-TEXT"


def _history(n, prefix="hist-marker"):
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"{prefix}-{i:02d} lorem",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Pure core: plan_compaction
# ---------------------------------------------------------------------------

def test_plan_below_threshold_is_noop():
    cfg = CompactionConfig(trigger_messages=40, trigger_chars=24_000, keep_recent=12)
    assert plan_compaction(_history(10), 0, cfg) is None
    # Char bound also respected: few messages, small content.
    assert plan_compaction(_history(3), 0, cfg) is None


def test_plan_char_trigger_compacts_few_huge_messages():
    msgs = [
        {"role": "user", "content": "x" * 30_000},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    cfg = CompactionConfig(trigger_messages=40, trigger_chars=24_000, keep_recent=1)
    plan = plan_compaction(msgs, 0, cfg)
    assert plan is not None
    assert (plan.fold_start, plan.fold_end) == (0, 2)
    assert plan.folded_count == 2


def test_plan_folds_prefix_and_keeps_recent_tail():
    cfg = CompactionConfig(trigger_messages=20, trigger_chars=10**9, keep_recent=6)
    plan = plan_compaction(_history(30), 0, cfg)
    assert plan is not None
    assert (plan.fold_start, plan.fold_end, plan.folded_count) == (0, 24, 24)


def test_plan_boundary_never_starts_tail_with_tool_result():
    msgs = [
        {"role": "user", "content": "go"},                                   # 0
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},   # 1
        {"role": "tool", "content": "result", "tool_call_id": "1"},          # 2
        {"role": "assistant", "content": "done"},                            # 3
        {"role": "user", "content": "next"},                                 # 4
        {"role": "assistant", "content": "ok"},                              # 5
    ]
    # Naive boundary (keep 4) would start the tail at the tool result (2);
    # the plan must walk back so the tail starts at its tool call (1).
    cfg = CompactionConfig(trigger_messages=3, trigger_chars=10**9, keep_recent=4)
    plan = plan_compaction(msgs, 0, cfg)
    assert plan is not None
    assert plan.fold_end == 1
    assert msgs[plan.fold_end]["role"] == "assistant"


def test_plan_nothing_new_to_fold_returns_none():
    # Marker already covers everything except the kept tail.
    cfg = CompactionConfig(trigger_messages=20, trigger_chars=10**9, keep_recent=12)
    assert plan_compaction(_history(45), 40, cfg) is None


# ---------------------------------------------------------------------------
# compact_history with a stub summariser
# ---------------------------------------------------------------------------

class _StubSummariser:
    def __init__(self, result="S"):
        self.result = result
        self.calls = []

    async def __call__(self, existing_summary, folded):
        self.calls.append({"existing": existing_summary, "folded": folded})
        return self.result


@pytest.mark.asyncio
async def test_compact_history_applies_plan():
    stub = _StubSummariser("S1")
    cfg = CompactionConfig(trigger_messages=20, trigger_chars=10**9, keep_recent=6)
    out = await compact_history(_history(30), None, 0, stub, cfg)
    assert out == ("S1", 24)
    assert stub.calls[0]["existing"] is None
    assert len(stub.calls[0]["folded"]) == 24


@pytest.mark.asyncio
async def test_compact_history_second_round_folds_only_new_turns():
    stub = _StubSummariser("S2")
    cfg = CompactionConfig(trigger_messages=20, trigger_chars=10**9, keep_recent=6)
    out = await compact_history(_history(44), "S1", 24, stub, cfg)
    assert out == ("S2", 38)
    # The summariser saw the existing summary and ONLY messages 24..37.
    assert stub.calls[0]["existing"] == "S1"
    folded = stub.calls[0]["folded"]
    assert len(folded) == 14
    assert folded[0]["content"].startswith("hist-marker-24")
    assert folded[-1]["content"].startswith("hist-marker-37")


@pytest.mark.asyncio
async def test_compact_history_empty_summary_does_not_advance_marker():
    stub = _StubSummariser("   ")
    cfg = CompactionConfig(trigger_messages=20, trigger_chars=10**9, keep_recent=6)
    assert await compact_history(_history(30), None, 0, stub, cfg) is None


# ---------------------------------------------------------------------------
# Model-facing view
# ---------------------------------------------------------------------------

def test_view_without_summary_is_the_full_history():
    msgs = _history(9)
    state = {"messages": msgs}
    assert model_facing_messages(state) == message_dicts(msgs)
    assert context_summary_section(state) == ""


def test_view_with_summary_is_tail_plus_summary_section():
    msgs = _history(10)
    state = {"messages": msgs, "context_summary": "S", "context_summary_upto": 7}
    view = model_facing_messages(state)
    assert view == message_dicts(msgs)[7:]
    assert view[0]["content"].startswith("hist-marker-07")
    section = context_summary_section(state)
    assert "S" in section and "7" in section


def test_supervisor_routing_prompt_uses_compacted_view():
    msgs = _history(10)
    state = {
        "messages": msgs,
        "context_summary": "S",
        "context_summary_upto": 7,
        "original_prompt": "go",
        "plan": "p",
    }
    prompt = supervisor_routing_prompt(state)
    system_text = prompt[0].content
    assert "Summary of the earlier conversation" in system_text
    contents = [
        m["content"] if isinstance(m, dict) else m.content for m in prompt[1:]
    ]
    assert contents == [m["content"] for m in msgs[7:]]


# ---------------------------------------------------------------------------
# Summariser construction / provider resolution / config
# ---------------------------------------------------------------------------

class _RecordingProvider:
    def __init__(self, text="the summary"):
        self.text = text
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({"node": node_name, "messages": [dict(m) for m in messages]})
        yield StreamChunk(text=self.text, finish_reason="stop")


@pytest.mark.asyncio
async def test_make_provider_summariser_collects_text_and_sends_turns():
    provider = _RecordingProvider()
    summariser = make_provider_summariser(provider)
    out = await summariser("previous summary", [{"role": "user", "content": "hello world"}])
    assert out == "the summary"
    assert provider.calls[0]["node"] == "compaction"
    sent = provider.calls[0]["messages"][0]["content"]
    assert "previous summary" in sent
    assert "hello world" in sent


def test_resolve_summariser_falls_back_to_run_provider():
    fallback = object()
    assert resolve_summariser_provider(None, None, fallback) is fallback
    cfg = UserConfig(auxiliary_models={"compression": "auto"})
    assert resolve_summariser_provider(cfg, object(), fallback) is fallback
    # An override without a secret store cannot be built either.
    cfg = UserConfig(auxiliary_models={"compression": "provX:::modelY"})
    assert resolve_summariser_provider(cfg, None, fallback) is fallback


def test_resolve_summariser_uses_compression_override(monkeypatch):
    sentinel = object()
    seen = {}

    def _fake_create(provider_name, model_name, secret_store):
        seen.update(provider=provider_name, model=model_name, store=secret_store)
        return sentinel

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", _fake_create)
    store = object()
    cfg = UserConfig(auxiliary_models={"compression": "provX:::modelY"})
    assert resolve_summariser_provider(cfg, store, object()) is sentinel
    assert seen == {"provider": "provX", "model": "modelY", "store": store}


def test_compaction_config_defaults_and_user_config_round_trip():
    assert compaction_config_from(None) == CompactionConfig(40, 24_000, 12)
    cfg = UserConfig(
        compaction_trigger_messages=7, compaction_trigger_chars=99,
        compaction_keep_recent=3)
    assert compaction_config_from(cfg) == CompactionConfig(7, 99, 3)
    # Persisted round-trip keeps the thresholds AND the compression
    # override, while still pruning genuinely seam-less tasks.
    restored = UserConfig.from_dict({
        **cfg.to_dict(),
        "auxiliary_models": {"compression": "p:::m", "title": "x:::y"},
    })
    assert restored.compaction_trigger_messages == 7
    assert restored.compaction_keep_recent == 3
    assert restored.auxiliary_models == {"compression": "p:::m"}


# ---------------------------------------------------------------------------
# Executor integration: compaction at run assembly
# ---------------------------------------------------------------------------

class CompactionGraphProvider:
    """Full-graph stub: supervisor routes to planner, planner answers,
    and the compaction node returns a fixed summary text."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "compaction":
            yield StreamChunk(text=SUMMARY_TEXT, finish_reason="stop")
        elif node_name == "planner":
            yield StreamChunk(text="PLANNER-ANSWER", finish_reason="stop")
        else:
            yield StreamChunk(
                text=('{"next": "planner", "reason": "compaction routing", '
                      '"briefing": ""}'),
                finish_reason="stop",
            )

    def calls_for(self, node):
        return [c for c in self.calls if c["node"] == node]


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Same isolation as the parity suite: tmp checkpoint DB, no real
    memory writes, no artifact files."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "compaction/artifact.txt")
    return tmp_path


async def _seed_thread(db_path, session_id, values):
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        await graph.aupdate_state(
            {"configurable": {"thread_id": session_id}}, values)


async def _thread_values(db_path, session_id):
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = compile_graph(checkpointer=saver)
        snap = await graph.aget_state({"configurable": {"thread_id": session_id}})
        return snap.values or {}


async def _run(provider, user_config, session_id, prompt="fresh question"):
    return [event async for event in executor_module.run_langgraph_task(
        prompt,
        provider,
        tools=ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config,
    )]


def _joined(call):
    return "\n".join(str(m.get("content") or "") for m in call["messages"])


@pytest.mark.asyncio
async def test_executor_compacts_long_history_and_preserves_store(graph_env):
    db_path = graph_env / "checkpoints.db"
    session_id = "compact-long"
    seeded = _history(30)
    await _seed_thread(db_path, session_id, {"messages": seeded})
    before = await _thread_values(db_path, session_id)
    assert len(before["messages"]) == 30

    provider = CompactionGraphProvider()
    cfg = UserConfig(
        compaction_trigger_messages=20,
        compaction_trigger_chars=24_000,
        compaction_keep_recent=6,
    )
    events = await _run(provider, cfg, session_id)
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]

    # The summariser folded exactly the first 24 turns.
    compaction_calls = provider.calls_for("compaction")
    assert len(compaction_calls) == 1
    summariser_input = _joined(compaction_calls[0])
    assert "hist-marker-00" in summariser_input
    assert "hist-marker-23" in summariser_input
    assert "hist-marker-24" not in summariser_input

    # State carries the summary + marker; the stored history is intact
    # (the run appended to it, but the seeded prefix is untouched).
    after = await _thread_values(db_path, session_id)
    assert after["context_summary"] == SUMMARY_TEXT
    assert after["context_summary_upto"] == 24
    assert len(after["messages"]) > 30
    prefix = message_dicts(after["messages"])[:30]
    assert prefix == message_dicts(seeded)

    # The planner saw summary (system context) + verbatim tail only.
    planner_input = _joined(provider.calls_for("planner")[-1])
    assert SUMMARY_TEXT in planner_input
    assert "hist-marker-24" in planner_input
    assert "hist-marker-29" in planner_input
    assert "hist-marker-23" not in planner_input
    assert "hist-marker-00" not in planner_input
    assert "fresh question" in planner_input


@pytest.mark.asyncio
async def test_executor_second_fold_is_incremental(graph_env):
    db_path = graph_env / "checkpoints.db"
    session_id = "compact-incremental"
    # A thread that already compacted once: 24 of 44 messages folded.
    await _seed_thread(db_path, session_id, {
        "messages": _history(44),
        "context_summary": SUMMARY_TEXT,
        "context_summary_upto": 24,
    })
    provider = CompactionGraphProvider()
    cfg = UserConfig(
        compaction_trigger_messages=20,
        compaction_trigger_chars=24_000,
        compaction_keep_recent=6,
    )
    await _run(provider, cfg, session_id)

    compaction_calls = provider.calls_for("compaction")
    assert len(compaction_calls) == 1
    summariser_input = _joined(compaction_calls[0])
    # Existing summary went in; only the newly aged turns were folded.
    assert SUMMARY_TEXT in summariser_input
    assert "hist-marker-30" in summariser_input
    assert "hist-marker-10" not in summariser_input
    assert "hist-marker-43" not in summariser_input

    after = await _thread_values(db_path, session_id)
    assert after["context_summary_upto"] == 38
    planner_input = _joined(provider.calls_for("planner")[-1])
    assert "hist-marker-37" not in planner_input
    assert "hist-marker-43" in planner_input


@pytest.mark.asyncio
async def test_executor_below_threshold_behaves_as_before(graph_env):
    db_path = graph_env / "checkpoints.db"
    session_id = "compact-short"
    await _seed_thread(db_path, session_id, {"messages": _history(6)})
    provider = CompactionGraphProvider()
    events = await _run(provider, UserConfig(), session_id)
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]

    assert provider.calls_for("compaction") == []
    after = await _thread_values(db_path, session_id)
    assert not after.get("context_summary")
    planner_input = _joined(provider.calls_for("planner")[-1])
    assert "hist-marker-00" in planner_input
    assert "hist-marker-05" in planner_input
