"""P1a — the supervisor emits the tier (orchestration programme, K-08).

Plan P1a, Option A (the briefing tag): the supervisor states the
tier a delegation needs by opening its briefing with a tag —
``[tier: small|standard|strong]`` — and that statement becomes
S14's primary complexity signal.

The contract these tests pin:

* the post-model hook parses and strips the tag where the
  briefing becomes the delegation log entry, on BOTH supervisor
  topologies (the legacy transfer call and the P1d delegation
  call), storing ``tier`` on the logged decision; the specialist
  only ever sees the stripped briefing (context_briefing AND the
  delegation call's briefing argument);
* a malformed tag, or a tag with an unknown tier value, is
  treated as absent — the decision passes through untouched;
* ``_complexity_need`` serves the explicit tier first (it beats
  keyword/length inference in both directions); untagged
  delegations infer exactly as before;
* the vision hard filter still binds an explicitly-tiered
  delegation — a stated tier chooses within the eligible pool,
  never outside it;
* ``RoutingChoice.tier_source`` records "explicit" vs "inferred"
  and rides the NODE_ACTIVITY payload; a need supplied through
  the P3a ``need_fn`` seam is never labelled the supervisor's
  mark.

The end-to-end seam (hook -> log -> specialist routing -> event)
is pinned by the S23 contract tests in
``tests/test_s23_supervisor_tier.py``.
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from aether_engine.langgraph import model_routing
from aether_engine.langgraph.model_routing import (
    History,
    TaskSignals,
    _complexity_need,
    collect_candidates,
    route,
    signals_for_state,
)
from aether_engine.langgraph.nodes.supervisor import _supervisor_system_prompt
from aether_engine.langgraph.supervisor_model import (
    parse_tier_tag,
    supervisor_post_model_hook,
)
from aether_engine.routing.registry import ModelRegistry

# ── The tag parser ────────────────────────────────────────────────


def test_parse_valid_tags():
    assert parse_tier_tag("[tier: strong] the facts") == ("strong", "the facts")
    assert parse_tier_tag("[tier:small]facts") == ("small", "facts")
    assert parse_tier_tag("  [tier: Standard]  facts") == ("standard", "facts")
    assert parse_tier_tag("[TIER: STRONG] facts") == ("strong", "facts")
    assert parse_tier_tag("[tier: strong]") == ("strong", "")


def test_parse_absent_and_malformed_tags_are_untouched():
    for briefing in (
        "plain briefing",
        "facts first, [tier: strong] later",   # not at the start
        "[tier: huge] facts",                  # unknown tier value
        "[tier:] facts",                       # no value
        "[tier: strong facts",                 # unclosed
        "[tier strong] facts",                 # no colon
        "",
    ):
        assert parse_tier_tag(briefing) == (None, briefing)
    assert parse_tier_tag(None) == (None, None)


# ── The hook: parse + strip on both topologies ────────────────────


def _legacy_message(decision):
    return AIMessage(
        content="routing",
        tool_calls=[{"name": "transfer_to_coder", "args": {},
                     "id": "call-legacy"}],
        additional_kwargs={"aether_decision": decision},
    )


def _tools_message(decision, briefing_arg):
    return AIMessage(
        content="routing",
        tool_calls=[{"name": "delegate_to_coder",
                     "args": {"briefing": briefing_arg},
                     "id": "call-tools", "type": "tool_call"}],
        additional_kwargs={"aether_decision": decision},
        id="msg-tools-1",
    )


def _hook(state):
    return supervisor_post_model_hook(state)


def test_hook_legacy_tagged_briefing_is_parsed_stripped_and_logged():
    decision = {"next": "coder", "reason": "build it",
                "briefing": "[tier: strong] the retrieved facts"}
    state = {"messages": [HumanMessage(content="x"), _legacy_message(decision)],
             "delegation_count": 0}
    update = _hook(state)
    logged = update["delegation_log"][0]["decision"]
    assert logged["tier"] == "strong"
    assert logged["briefing"] == "the retrieved facts"
    assert update["context_briefing"] == "the retrieved facts"
    assert update["delegation_count"] == 1
    # Legacy handoff calls carry no briefing argument: no message
    # rewrite is needed (or produced).
    assert "messages" not in update


def test_hook_tools_tagged_briefing_strips_the_call_argument_too():
    decision = {"next": "coder", "reason": "build it",
                "briefing": "[tier: small] the retrieved facts"}
    state = {"messages": [
        HumanMessage(content="x"),
        _tools_message(decision, "[tier: small] the retrieved facts"),
    ], "delegation_count": 0}
    update = _hook(state)
    logged = update["delegation_log"][0]["decision"]
    assert logged["tier"] == "small"
    assert logged["briefing"] == "the retrieved facts"
    assert update["context_briefing"] == "the retrieved facts"
    # The pending message is replaced in place (same id) with the
    # tag stripped from the delegation call's briefing argument —
    # the tool receives, and the specialist's history records, the
    # stripped briefing.
    (rewritten,) = update["messages"]
    assert rewritten.id == "msg-tools-1"
    assert rewritten.tool_calls[0]["args"]["briefing"] == "the retrieved facts"


def test_hook_untagged_decision_is_byte_identical():
    decision = {"next": "coder", "reason": "build it",
                "briefing": "plain facts"}
    state = {"messages": [
        HumanMessage(content="x"),
        _tools_message(decision, "plain facts"),
    ], "delegation_count": 0}
    update = _hook(state)
    logged = update["delegation_log"][0]["decision"]
    assert logged == decision
    assert "tier" not in logged
    assert "messages" not in update
    assert update["context_briefing"] == "plain facts"


def test_hook_invalid_tier_value_is_treated_as_absent():
    decision = {"next": "coder", "reason": "build it",
                "briefing": "[tier: huge] the facts"}
    state = {"messages": [HumanMessage(content="x"), _legacy_message(decision)],
             "delegation_count": 0}
    update = _hook(state)
    logged = update["delegation_log"][0]["decision"]
    assert "tier" not in logged
    assert logged["briefing"] == "[tier: huge] the facts"
    assert update["context_briefing"] == "[tier: huge] the facts"


def test_hook_tag_only_briefing_still_gets_memory_reconstruction():
    """A briefing that is ONLY a tag: the tier is kept, and the
    (now empty) briefing is reconstructed from memory results
    exactly as an empty stamped briefing is today."""
    decision = {"next": "coder", "reason": "build it",
                "briefing": "[tier: strong]"}
    state = {"messages": [
        HumanMessage(content="x"),
        ToolMessage(content="a remembered fact", tool_call_id="mem-1"),
        _tools_message(decision, "[tier: strong]"),
    ], "delegation_count": 0}
    update = _hook(state)
    logged = update["delegation_log"][0]["decision"]
    assert logged["tier"] == "strong"
    assert logged["briefing"] == "a remembered fact"
    assert update["context_briefing"] == "a remembered fact"


# ── The prompt ────────────────────────────────────────────────────


def test_routing_prompt_teaches_the_tier_tag_in_both_topologies():
    for kwargs in ({"tool_mode": True, "delegation_tools": True},
                   {"tool_mode": True, "delegation_tools": False},
                   {"tool_mode": False}):
        text = _supervisor_system_prompt(
            "the request", "No plan yet.", ["researcher", "planner", "coder"],
            **kwargs)
        assert "[tier: small] = mechanical or short work" in text
        assert "[tier: standard] = ordinary specialist work" in text
        assert "[tier: strong] = deep reasoning, hard debugging, architecture" in text
        assert "stripped before the specialist sees the briefing" in text


# ── Signals ───────────────────────────────────────────────────────


def _state_with_decision(decision):
    return {
        "messages": [{"role": "user", "content": "do the work"}],
        "original_prompt": "do the work",
        "delegation_log": [
            {"agent": "supervisor", "timestamp": 1.0, "decision": decision},
        ],
    }


def test_signals_carry_the_stored_tier():
    signals = signals_for_state("coder", _state_with_decision(
        {"next": "coder", "reason": "r", "briefing": "b", "tier": "strong"}))
    assert signals.explicit_tier == model_routing.TIER_STRONG


def test_signals_without_a_valid_tier_have_no_statement():
    for decision in (
        {"next": "coder", "reason": "r", "briefing": "b"},
        {"next": "coder", "reason": "r", "briefing": "b", "tier": "huge"},
        {"next": "coder", "reason": "r", "briefing": "b", "tier": None},
    ):
        signals = signals_for_state("coder", _state_with_decision(decision))
        assert signals.explicit_tier is None


def test_signals_denied_entry_carries_no_tier():
    signals = signals_for_state("coder", _state_with_decision(
        {"next": "coder", "reason": "r", "briefing": "b",
         "tier": "strong", "denied": True}))
    assert signals.explicit_tier is None


# ── The need: explicit wins, inference is the fallback ────────────


def test_explicit_tier_beats_keyword_inference_both_ways():
    reasoning_text = "Prove the derivation, step by step, and check every case"
    need, why = _complexity_need(
        TaskSignals(agent="coder", task_text=reasoning_text,
                    explicit_tier=model_routing.TIER_SMALL),
        incumbent_tier=model_routing.TIER_STANDARD)
    assert need == model_routing.TIER_SMALL
    assert why == "the supervisor marked this delegation small tier"

    need, why = _complexity_need(
        TaskSignals(agent="researcher", task_text="capital of France?",
                    explicit_tier=model_routing.TIER_STRONG),
        incumbent_tier=model_routing.TIER_SMALL)
    assert need == model_routing.TIER_STRONG
    assert why == "the supervisor marked this delegation strong tier"


def test_untagged_need_inference_is_unchanged():
    need, why = _complexity_need(
        TaskSignals(agent="planner",
                    task_text="Prove the derivation, step by step"),
        incumbent_tier=model_routing.TIER_STANDARD)
    assert need == model_routing.TIER_STRONG
    assert why == "the task calls for deep reasoning"

    need, why = _complexity_need(
        TaskSignals(agent="coder", task_text="fix the bug in the login script"),
        incumbent_tier=model_routing.TIER_STANDARD)
    assert need == model_routing.TIER_STANDARD
    assert why == "a standard task"


# ── The pure router with an explicit tier ─────────────────────────


class _FakeHealth:
    def __init__(self, unavailable=()):
        self._unavailable = set(unavailable)

    def is_provider_available(self, provider):
        return provider not in self._unavailable


def _candidates(configured=("google_gemini",),
                incumbent=("google_gemini", "gemini-3.5-flash")):
    return collect_candidates(
        registry=ModelRegistry(),
        configured_providers=list(configured),
        health_manager=_FakeHealth(),
        incumbent_provider=incumbent[0],
        incumbent_model=incumbent[1])


def test_route_serves_the_explicit_tier_and_labels_it():
    choice = route(
        TaskSignals(agent="researcher", task_text="capital of France?",
                    explicit_tier=model_routing.TIER_STRONG),
        _candidates(), History())
    assert (choice.provider, choice.model) == ("google_gemini", "gemini-2.5-pro")
    assert choice.tier == model_routing.TIER_STRONG
    assert choice.tier_source == "explicit"
    assert "supervisor marked this delegation strong tier" in choice.reason


def test_route_without_a_tag_infers_and_labels_it_inferred():
    choice = route(
        TaskSignals(agent="researcher", task_text="capital of France?"),
        _candidates(), History())
    assert choice.tier == model_routing.TIER_SMALL
    assert choice.tier_source == "inferred"
    assert "supervisor marked" not in choice.reason


def test_route_need_fn_answer_is_never_labelled_explicit():
    """The P3a seam: when a need_fn supplies the need, the choice
    is the scorer's, not the supervisor's mark — even when the
    delegation also carries a tag."""
    choice = route(
        TaskSignals(agent="researcher", task_text="capital of France?",
                    explicit_tier=model_routing.TIER_STRONG),
        _candidates(), History(),
        need_fn=lambda signals, incumbent: (
            model_routing.TIER_STANDARD, "a learned need"))
    # The need_fn's standard need was served (nearest available
    # tier — this pool holds no standard-tier model), NOT the
    # supervisor's strong mark, and the source says so.
    assert choice.tier != model_routing.TIER_STRONG
    assert choice.reason.startswith("A learned need")
    assert choice.tier_source == "inferred"


def test_explicit_tier_never_breaks_the_vision_hard_filter():
    candidates = _candidates(
        configured=("google_gemini", "deepseek"),
        incumbent=("deepseek", "deepseek-chat"))
    choice = route(
        TaskSignals(agent="researcher", task_text="what is in this picture?",
                    has_images=True,
                    explicit_tier=model_routing.TIER_SMALL),
        candidates, History())
    assert choice is not None
    entry = ModelRegistry().get_model(
        choice.model, provider=choice.provider)
    assert "vision" in entry.capabilities
