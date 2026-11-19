"""P1d Step 0 spike — subagents-as-tools mechanics on langgraph 1.2.16.

Question (plan p1d, preservation items 1/4/5): can a delegation tool
inside a ``create_react_agent`` supervisor invoke the REAL researcher
specialist subgraph (built by ``graph._build_specialist_graph``, with
its policy -> approval-pause -> execute pipeline) and have

  (a) an approval ``interrupt()`` raised inside the specialist bubble
      out through the tool call to the OUTER graph, and resume back
      into the same specialist (the tool executed exactly once, the
      specialist continuing from its post-pause round), and
  (b) the message-history semantics the current supervisor relies on:
      the specialist receives the full conversation plus the
      ``context_briefing`` the post-model hook wrote, and its full
      message trail (internal tool result + final answer) lands in
      the supervisor's history where the supervisor's next model step
      can see the answer as an assistant message?

Two specialist compile variants are tried, per the plan's risk note:
  A — subgraph compiled WITH the shared checkpointer (today's build).
  B — subgraph compiled WITHOUT a checkpointer (only the outer graph
      holds one; the configuration the migration research describes).

Run:  uv run python spike_p1d/spike.py
Exit code 0 when at least one variant passes every check.
"""

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolRuntime, create_react_agent
from langgraph.types import Command
from pydantic import Field

from aether_engine.config import UserConfig
from aether_engine.langgraph.graph import _build_specialist_graph
from aether_engine.langgraph.nodes import researcher as researcher_module
from aether_engine.langgraph.nodes.researcher import researcher_node
from aether_engine.langgraph.state import AetherState, message_dicts
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

ANSWER = "SPIKE RESEARCH ANSWER"
BRIEFING = "SPIKE BRIEFING"
PROBE_RESULT = "probe-result-spike"

# The specialist writes an artifact on its final round; keep the
# spike off the real artifact store (the documented patch seam).
researcher_module.save_artifact = lambda *a, **k: "/tmp/spike_artifact.txt"


class ResearcherProvider:
    """Scripted provider for the specialist node: round 1 calls the
    ask-policy probe tool, round 2 answers with text."""

    provider_name = "spike"
    model = "spike-model"

    def __init__(self):
        self.calls: list[dict] = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
            "node_name": node_name,
        })
        rounds = sum(1 for c in self.calls if c["node_name"] == "researcher")
        if node_name == "researcher" and rounds == 1:
            yield StreamChunk(
                tool_calls=[ToolCall(name="spike_probe",
                                     args={"q": "x"}, call_id="probe-1")],
                finish_reason="tool_calls")
        elif node_name == "researcher":
            yield StreamChunk(text=ANSWER, finish_reason="stop")
        else:
            yield StreamChunk(text="unused", finish_reason="stop")


class StubSupervisorModel(BaseChatModel):
    """First step delegates to the researcher; once the researcher's
    answer is in view (an assistant message), it finishes."""

    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "spike-stub-supervisor"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("async only")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        dicts = message_dicts(messages)
        if any(m.get("role") == "assistant" and m.get("content") == ANSWER
               for m in dicts):
            message = AIMessage(content="SPIKE FINAL")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{
                    "name": "delegate_to_researcher",
                    "args": {"briefing": "spike briefing arg"},
                    "id": "spike-delegate-1",
                    "type": "tool_call",
                }],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def spike_post_model_hook(state: dict) -> dict:
    """Minimal stand-in for supervisor_post_model_hook: on a delegate
    call, write the briefing + counter exactly where the real hook
    writes them, so the spike proves hook -> tool -> subgraph flow."""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage):
        for tc in last.tool_calls or []:
            if tc.get("name") == "delegate_to_researcher":
                return {
                    "context_briefing": BRIEFING,
                    "delegation_count": int(state.get("delegation_count") or 0) + 1,
                    "delegation_log": [{
                        "agent": "supervisor",
                        "timestamp": time.time(),
                        "decision": {"next": "researcher",
                                     "reason": "spike",
                                     "briefing": BRIEFING},
                    }],
                    "active_specialist": "researcher",
                }
    return {}


# Channels a specialist subgraph may write that the delegation tool
# merges back into the supervisor's state (messages handled apart).
MERGED_CHANNELS = (
    "blackboard", "delegation_log", "plan", "current_phase",
    "artifact_paths", "tool_results_summary", "pending_tool_call",
    "queued_tool_calls", "tool_call_signature", "tool_call_repeats",
    "write_tool_used", "structured_outputs", "subagent_count",
    "context_summary", "context_summary_upto",
)


def make_delegate_tool(specialist_graph):
    @tool("delegate_to_researcher",
          description="Delegate a research task to the researcher.")
    async def delegate_to_researcher(briefing: str, runtime: ToolRuntime) -> Command:
        state = dict(runtime.state or {})
        sub_input = {k: v for k, v in state.items() if k != "remaining_steps"}
        try:
            final = await specialist_graph.ainvoke(sub_input, config=runtime.config)
        except BaseException as exc:  # noqa: BLE001 — spike diagnostics
            print(f"  [tool] subgraph ainvoke raised {type(exc).__name__}: "
                  f"{str(exc)[:200]}")
            raise
        print(f"  [tool] subgraph returned; interrupts="
              f"{final.get('__interrupt__')!r}")
        print(f"  [tool] final messages: "
              f"{message_dicts(final.get('messages') or [])}")
        print(f"  [tool] pending_tool_call: {final.get('pending_tool_call')!r}")

        input_messages = list(state.get("messages") or [])
        final_messages = list(final.get("messages") or [])
        if final_messages[: len(input_messages)] == input_messages:
            delta = final_messages[len(input_messages):]
        else:
            delta = final_messages
        answer = ""
        for msg in reversed(message_dicts(delta)):
            if msg.get("role") == "assistant" and msg.get("content"):
                answer = str(msg["content"])
                break
        tool_message = ToolMessage(
            content=answer or "Delegation complete.",
            name="delegate_to_researcher",
            tool_call_id=runtime.tool_call_id,
        )
        # Two Commands, not one: the state channel's reducer converts
        # any multi-message write to plain dicts, and the agent's
        # chat-history validation only recognises BaseMessage objects
        # — a ToolMessage batched with the specialist's dict messages
        # would become a dict and the delegate call would read as
        # unanswered. A single-message write stays a ToolMessage.
        channels: dict[str, Any] = {}
        for key in MERGED_CHANNELS:
            if key in final:
                channels[key] = final[key]
        return [
            Command(update={"messages": [tool_message]}),
            Command(update={"messages": delta, **channels}),
        ]

    return delegate_to_researcher


def initial_state() -> dict:
    return {
        "original_prompt": "research the spike",
        "messages": [{"role": "user", "content": "research the spike"}],
        "plan": "No plan currently.",
        "current_phase": "initializing",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "queued_tool_calls": [],
        "active_specialist": None,
        "blackboard": {},
        "structured_outputs": {},
        "write_tool_used": False,
        "plan_mode": False,
        "delegation_log": [],
        "delegation_count": 0,
        "subagent_count": 0,
        "task_id": "spike-task",
        "session_id": "spike-session",
        "message_id": None,
        "context_briefing": None,
        "active_skills": [],
        "run_config": None,
    }


async def run_variant(variant: str, db_dir: Path) -> dict[str, Any]:
    """One full run: delegate -> approval interrupt -> resume -> done."""
    checks: dict[str, Any] = {}
    executed: list[str] = []

    def _probe(q: str = "") -> str:
        executed.append(q)
        return PROBE_RESULT

    registry = ToolRegistry()
    registry.register(Tool(
        name="spike_probe",
        description="Spike probe tool (ask-policy, read-only execution).",
        input_schema={"type": "object",
                      "properties": {"q": {"type": "string"}}},
        execute_fn=_probe,
        risk=ToolRisk.READ_ONLY,
    ))
    provider = ResearcherProvider()
    model = StubSupervisorModel()
    user_config = UserConfig(tool_policies={"spike_probe": "Require Approval"})

    db_path = db_dir / f"spike-{variant}.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        checkpointer = saver if variant == "A" else None
        specialist = _build_specialist_graph(
            "researcher", researcher_node, checkpointer)
        agent = create_react_agent(
            model=model,
            tools=[make_delegate_tool(specialist)],
            prompt=lambda state: list(state["messages"]),
            state_schema=AetherState,
            post_model_hook=spike_post_model_hook,
            name="supervisor",
        )
        builder = StateGraph(AetherState)
        builder.add_node("supervisor", agent)
        builder.add_edge(START, "supervisor")
        builder.add_edge("supervisor", END)
        graph = builder.compile(checkpointer=saver)

        config = {
            "recursion_limit": 25,
            "configurable": {
                "thread_id": f"spike-thread-{variant}",
                "provider": provider,
                "tool_registry": registry,
                "user_config": user_config,
                "agent_models": {},
                "agent_tools": {},
                "disabled_nodes": set(),
                "workspace_roots": [],
                "request_id": "spike-req",
                "session_id": "spike-session",
                "task_class": "standard",
            },
        }

        first = await graph.ainvoke(initial_state(), config=config)
        interrupts = first.get("__interrupt__") or []
        checks["a_interrupt_bubbled"] = bool(interrupts) and any(
            (getattr(i, "value", None) or {}).get("type") == "approval_request"
            and (getattr(i, "value", None) or {}).get("tool_name") == "spike_probe"
            for i in interrupts)
        checks["a_interrupt_payload"] = [
            getattr(i, "value", None) for i in interrupts]

        if not checks["a_interrupt_bubbled"]:
            checks["error"] = "no approval interrupt surfaced on the outer graph"
            return checks

        final = await graph.ainvoke(
            Command(resume={"approved": True}), config=config)
        checks["a_resume_completed"] = "__interrupt__" not in final
        checks["a_probe_executed_once"] = executed == ["x"]

        researcher_calls = [c for c in provider.calls
                            if c["node_name"] == "researcher"]
        checks["a_specialist_rounds"] = len(researcher_calls)
        if len(researcher_calls) >= 2:
            second = message_dicts(researcher_calls[1]["messages"])
            checks["a_resume_into_specialist"] = any(
                m.get("role") == "tool" and PROBE_RESULT in str(m.get("content"))
                for m in second)
        else:
            checks["a_resume_into_specialist"] = False

        if researcher_calls:
            first_call = researcher_calls[0]["messages"]
            system_text = ""
            if first_call and isinstance(first_call[0], dict):
                system_text = str(first_call[0].get("content") or "")
            checks["b_specialist_saw_briefing"] = BRIEFING in system_text
            checks["b_specialist_saw_history"] = any(
                isinstance(m, dict) and m.get("role") == "user"
                and "research the spike" in str(m.get("content"))
                for m in first_call)
        else:
            checks["b_specialist_saw_briefing"] = False
            checks["b_specialist_saw_history"] = False

        if len(model.seen) >= 2:
            second_view = message_dicts(model.seen[1])
            checks["b_supervisor_saw_answer"] = any(
                m.get("role") == "assistant" and m.get("content") == ANSWER
                for m in second_view)
            checks["b_supervisor_saw_full_history"] = any(
                m.get("role") == "tool" and PROBE_RESULT in str(m.get("content"))
                for m in second_view)
        else:
            checks["b_supervisor_saw_answer"] = False
            checks["b_supervisor_saw_full_history"] = False

        checks["b_delegation_count"] = final.get("delegation_count")
        checks["final_answer"] = None
        for msg in reversed(message_dicts(final.get("messages") or [])):
            if msg.get("role") == "assistant" and msg.get("content"):
                checks["final_answer"] = msg["content"]
                break
    return checks


async def main() -> int:
    verdicts: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for variant in ("A", "B"):
            try:
                verdicts[variant] = await run_variant(variant, Path(tmp))
            except Exception as exc:  # noqa: BLE001 — the spike reports, not crashes
                import traceback
                verdicts[variant] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
    required = (
        "a_interrupt_bubbled", "a_resume_completed",
        "a_probe_executed_once", "a_resume_into_specialist",
        "b_specialist_saw_briefing", "b_specialist_saw_history",
        "b_supervisor_saw_answer", "b_supervisor_saw_full_history",
    )
    ok_any = False
    for variant, checks in verdicts.items():
        print(f"=== Variant {variant} "
              f"({'shared checkpointer on subgraph' if variant == 'A' else 'outer graph only holds the checkpointer'}) ===")
        for key, value in checks.items():
            if key in ("a_interrupt_payload",):
                continue
            print(f"  {key}: {value}")
        passed = all(checks.get(k) is True for k in required) \
            and checks.get("b_delegation_count") == 1
        print(f"  VARIANT {variant}: {'PASS' if passed else 'FAIL'}")
        ok_any = ok_any or passed
    print("SPIKE VERDICT:", "PASS" if ok_any else "FAIL")
    return 0 if ok_any else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
