"""Golden characterisation corpus for P1c — One Signals Module.

These tests capture the CURRENT outputs of:
  - policy.infer_task_requirements (wants_code / wants_reasoning / has_images / low_latency)
  - model_routing._complexity_need (via TaskSignals — tier + reason)
  - intent.work_heuristic_reason and intent.fallback_chat_reason

over a fixed corpus of ≥40 prompts.

Acceptance bar: all tests must pass UNCHANGED after the refactor.
They are the proof of zero behaviour change.

Corpus covers:
  - plain chat and greetings
  - code requests
  - reasoning requests
  - image-less vs image cases (for infer_task_requirements)
  - multi-step lists
  - length edge cases: 139/140/141 and 1499/1500/1501 chars
    (thresholds differ per consumer — intent uses 1200/500/80;
     model_routing uses 1500/140)
"""

from aether_engine.langgraph.model_routing import (
    TIER_SMALL,
    TIER_STANDARD,
    TIER_STRONG,
    TaskSignals,
    _complexity_need,
)
from aether_engine.routing.intent import fallback_chat_reason, work_heuristic_reason
from aether_engine.routing.policy import infer_task_requirements

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig(text: str, has_images: bool = False) -> TaskSignals:
    return TaskSignals(agent="test_agent", task_text=text, has_images=has_images)


def _cn(text: str, incumbent_tier: int = TIER_STANDARD) -> tuple[int, str]:
    return _complexity_need(_sig(text), incumbent_tier)


# ---------------------------------------------------------------------------
# 1. policy.infer_task_requirements
# ---------------------------------------------------------------------------

class TestInferTaskRequirements:
    """Golden outputs of infer_task_requirements over the corpus."""

    # --- plain chat / greetings ---

    def test_01_plain_greeting(self):
        r = infer_task_requirements("Hello! How are you?")
        assert r.wants_code is False
        assert r.wants_reasoning is False
        assert r.has_images is False
        assert r.low_latency is True

    def test_02_simple_question(self):
        r = infer_task_requirements("What is the capital of France?")
        # Note: "capital" contains "api", triggering wants_code=True under current keyword scanning
        assert r.wants_code is True
        assert r.wants_reasoning is False
        assert r.low_latency is True

    def test_03_thanks(self):
        r = infer_task_requirements("Thanks, that was helpful!")
        assert r.wants_code is False
        assert r.wants_reasoning is False
        assert r.low_latency is True

    def test_04_conversation(self):
        r = infer_task_requirements("Tell me a joke about penguins.")
        assert r.wants_code is False
        assert r.wants_reasoning is False
        assert r.low_latency is True

    # --- code requests ---

    def test_05_code_keyword_function(self):
        r = infer_task_requirements("Write a function to sort a list in Python.")
        assert r.wants_code is True
        assert r.wants_reasoning is False
        assert r.low_latency is True

    def test_06_code_keyword_def(self):
        r = infer_task_requirements("def add(a, b): can you complete this?")
        assert r.wants_code is True

    def test_07_code_keyword_class(self):
        r = infer_task_requirements("Write a class  for a bank account.")
        assert r.wants_code is True

    def test_08_code_keyword_bug(self):
        r = infer_task_requirements("There is a bug in my code, please fix it.")
        assert r.wants_code is True

    def test_09_code_keyword_refactor(self):
        r = infer_task_requirements("Please refactor this module.")
        assert r.wants_code is True

    def test_10_code_keyword_sql(self):
        r = infer_task_requirements("Write a sql query to find duplicates.")
        assert r.wants_code is True

    def test_11_code_keyword_html(self):
        r = infer_task_requirements("Create an html page with a login form.")
        assert r.wants_code is True

    def test_12_code_keyword_api(self):
        r = infer_task_requirements("Design an api endpoint for user auth.")
        assert r.wants_code is True

    def test_13_code_fence(self):
        r = infer_task_requirements("Here is my code:\n```python\nprint('hi')\n```")
        assert r.wants_code is True

    def test_14_code_keyword_traceback(self):
        r = infer_task_requirements("I see a traceback error, what does it mean?")
        assert r.wants_code is True

    def test_15_code_keyword_algorithm(self):
        r = infer_task_requirements("Explain the algorithm for quicksort.")
        assert r.wants_code is True

    # --- reasoning requests ---

    def test_16_reasoning_step_by_step(self):
        r = infer_task_requirements("Think step by step and solve this.")
        assert r.wants_reasoning is True
        assert r.low_latency is False

    def test_17_reasoning_deep_think(self):
        r = infer_task_requirements("Please deep think about the implications.")
        assert r.wants_reasoning is True

    def test_18_reasoning_math_proof(self):
        r = infer_task_requirements("Write a math proof that sqrt(2) is irrational.")
        assert r.wants_reasoning is True

    def test_19_reasoning_logic_puzzle(self):
        r = infer_task_requirements("Solve this logic puzzle: ...")
        assert r.wants_reasoning is True

    def test_20_reasoning_benchmark_comparison(self):
        r = infer_task_requirements("Give me a benchmark comparison of GPT vs Gemini.")
        assert r.wants_reasoning is True

    def test_21_reasoning_derivation(self):
        r = infer_task_requirements("Show the derivation of the quadratic formula.")
        assert r.wants_reasoning is True

    def test_22_reasoning_complex_analysis(self):
        r = infer_task_requirements("Do a complex analysis of market trends.")
        assert r.wants_reasoning is True

    def test_23_reasoning_solve_riddle(self):
        r = infer_task_requirements("Can you solve riddle: I have cities, but no houses live there.")
        assert r.wants_reasoning is True

    # --- image cases ---

    def test_24_no_images(self):
        r = infer_task_requirements("What do you see?", images=None)
        assert r.has_images is False

    def test_25_with_images(self):
        r = infer_task_requirements("What do you see?", images=["photo.jpg"])
        assert r.has_images is True

    def test_26_with_multiple_images(self):
        r = infer_task_requirements("Compare these diagrams.", images=["a.png", "b.png"])
        assert r.has_images is True

    # --- multi-step ---

    def test_27_multi_step_list(self):
        r = infer_task_requirements(
            "1. Open the file\n2. Read the contents\n3. Write the output"
        )
        # multi-step list alone does not force wants_code or wants_reasoning
        assert r.wants_code is False
        assert r.wants_reasoning is False

    # --- mixed signals ---

    def test_28_code_and_reasoning(self):
        # Both keywords present: both flags set
        r = infer_task_requirements(
            "Step by step, write a complex algorithm for shortest paths."
        )
        assert r.wants_code is True
        assert r.wants_reasoning is True

    # --- prefer_local propagation ---

    def test_29_prefer_local_propagated(self):
        r = infer_task_requirements("Hello", prefer_local=True)
        assert r.prefer_local is True

    def test_30_prefer_local_default_false(self):
        r = infer_task_requirements("Hello")
        assert r.prefer_local is False

    # --- length edge cases (intent thresholds: 1200 / 500 / 80) ---
    # infer_task_requirements does NOT use length directly — only keyword checks.
    # These prompts exercise keyword absence with varying lengths.

    def test_31_short_plain_139_chars(self):
        # 139-char plain prompt — no keywords
        prompt = "A" * 139
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False

    def test_32_short_plain_140_chars(self):
        prompt = "A" * 140
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False

    def test_33_short_plain_141_chars(self):
        prompt = "A" * 141
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False

    def test_34_long_plain_1499_chars(self):
        prompt = "A" * 1499
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False

    def test_35_long_plain_1500_chars(self):
        prompt = "A" * 1500
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False

    def test_36_long_plain_1501_chars(self):
        prompt = "A" * 1501
        r = infer_task_requirements(prompt)
        assert r.wants_code is False
        assert r.wants_reasoning is False


# ---------------------------------------------------------------------------
# 2. model_routing._complexity_need (via TaskSignals)
# ---------------------------------------------------------------------------

class TestComplexityNeed:
    """Golden outputs of _complexity_need over the corpus."""

    def test_cn_01_plain_chat_standard(self):
        tier, reason = _cn("Hello, how are you?")
        assert tier == TIER_SMALL
        assert "short" in reason.lower()

    def test_cn_02_reasoning_keyword(self):
        tier, reason = _cn("Please reason step by step through this problem.")
        assert tier == TIER_STRONG
        assert "reasoning" in reason.lower()

    def test_cn_03_long_text_above_1500(self):
        # > LONG_TASK_CHARS (1500) → one tier up from standard
        text = "Review this document. " * 80  # well over 1500 chars
        tier, reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STRONG  # standard + 1
        assert "long" in reason.lower() or "multi-step" in reason.lower()

    def test_cn_04_long_text_above_1500_from_small(self):
        # incumbent small → max(SMALL, STANDARD) + 1 = STRONG
        text = "Explain this. " * 120
        tier, _reason = _cn(text, incumbent_tier=TIER_SMALL)
        assert tier == TIER_STRONG

    def test_cn_05_long_text_above_1500_caps_at_strong(self):
        # incumbent already strong → capped at TIER_STRONG
        text = "Long text. " * 150
        tier, _reason = _cn(text, incumbent_tier=TIER_STRONG)
        assert tier == TIER_STRONG

    def test_cn_06_short_no_code(self):
        # < SHORT_TASK_CHARS (140), no code keywords → TIER_SMALL
        text = "hi"
        tier, reason = _cn(text)
        assert tier == TIER_SMALL
        assert "short" in reason.lower()

    def test_cn_07_short_with_code_keyword(self):
        # < 140 chars but has a code keyword → not demoted to small
        text = "fix the bug"
        tier, _reason = _cn(text)
        # Has 'bug' — a code keyword — so stays at incumbent (TIER_STANDARD)
        assert tier == TIER_STANDARD

    def test_cn_08_exactly_140_chars_no_code(self):
        # Exactly 140 chars, no code keywords — len < 140 is False → standard
        text = "A" * 140
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        # len(text) < 140 is False (140 == 140, not less), stays standard
        assert tier == TIER_STANDARD

    def test_cn_09_exactly_139_chars_no_code(self):
        # 139 chars — len < 140 is True, no code kw → TIER_SMALL
        text = "A" * 139
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_SMALL

    def test_cn_10_exactly_141_chars_no_code(self):
        # 141 chars — len < 140 is False → stays standard
        text = "A" * 141
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STANDARD

    def test_cn_11_exactly_1499_chars_no_code(self):
        # 1499 chars — len > 1500 is False → check step markers (none) → standard
        text = "A" * 1499
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STANDARD

    def test_cn_12_exactly_1500_chars(self):
        # 1500 chars — len > 1500 is False → standard
        text = "A" * 1500
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STANDARD

    def test_cn_13_exactly_1501_chars(self):
        # 1501 chars — len > 1500 is True → up one tier
        text = "A" * 1501
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STRONG

    def test_cn_14_multi_step_markers(self):
        # ≥3 step markers → up one tier from incumbent
        text = "1. Do this\n2. Then that\n3. Finally this"
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STRONG

    def test_cn_15_two_step_markers_not_enough(self):
        # only 2 step markers (< 3), len < 140, no code kw → TIER_SMALL
        text = "1. Do this\n2. Then that"
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_SMALL

    def test_cn_16_step_prefix_format(self):
        # "step N" format also counts
        text = "step 1 do this\nstep 2 do that\nstep 3 finish"
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STRONG

    def test_cn_17_reasoning_beats_length(self):
        # Reasoning keyword wins unconditionally
        text = "reasoning " + "X" * 2000
        tier, _reason = _cn(text, incumbent_tier=TIER_SMALL)
        assert tier == TIER_STRONG

    def test_cn_18_code_task_short(self):
        # Short text, but has code keyword → not small tier
        text = "fix the bug"
        tier, _reason = _cn(text, incumbent_tier=TIER_STANDARD)
        assert tier == TIER_STANDARD  # incumbent stays; code kw blocks demotion

    def test_cn_19_incumbent_small_long_task(self):
        text = "Long description. " * 90
        tier, _reason = _cn(text, incumbent_tier=TIER_SMALL)
        assert tier == TIER_STRONG  # min(TIER_STRONG, max(SMALL, STANDARD)+1) = STRONG

    def test_cn_20_deep_think_keyword(self):
        tier, _reason = _cn("deep think about the problem carefully")
        assert tier == TIER_STRONG


# ---------------------------------------------------------------------------
# 3. intent.work_heuristic_reason and intent.fallback_chat_reason
# ---------------------------------------------------------------------------

class TestWorkHeuristicReason:
    """Golden outputs of work_heuristic_reason."""

    def test_wh_01_code_fence(self):
        assert work_heuristic_reason("Here is my code:\n```python\nprint('x')\n```") == "contains a code block"

    def test_wh_02_file_reference_py(self):
        assert work_heuristic_reason("Edit main.py please") == "references a file"

    def test_wh_03_file_reference_path(self):
        assert work_heuristic_reason("Read the file /home/user/config.yaml") == "references a file"

    def test_wh_04_work_verb_object(self):
        result = work_heuristic_reason("write a script to process the data")
        assert result == "imperative tool-shaped request"

    def test_wh_05_web_search(self):
        # Matches _WORK_VERB_OBJECT_RE ('search' ... 'web') before _WORK_PHRASE_RES
        result = work_heuristic_reason("search the web for latest AI news")
        assert result == "imperative tool-shaped request"

    def test_wh_06_latest_news(self):
        result = work_heuristic_reason("what is the latest news today?")
        assert result == "asks for web or current information"

    def test_wh_07_multi_step_numbered(self):
        prompt = "1. First do this\n2. Then do that\nAnd finally finish."
        result = work_heuristic_reason(prompt)
        # 2 step markers — but the condition is "findall >= 2" → multi-step
        assert result == "multi-step request"

    def test_wh_08_long_prompt(self):
        # > 1200 chars, no other triggers → long structured request
        prompt = "Explain " + "this " * 250  # well over 1200
        result = work_heuristic_reason(prompt)
        assert result == "long structured request"

    def test_wh_09_plain_chat_none(self):
        assert work_heuristic_reason("Hello, how are you?") is None

    def test_wh_10_simple_question_none(self):
        assert work_heuristic_reason("What is 2 + 2?") is None

    def test_wh_11_exactly_at_1200_boundary(self):
        # exactly 1200 chars — len > 1200 is False → None (no other triggers)
        prompt = "A" * 1200
        assert work_heuristic_reason(prompt) is None

    def test_wh_12_just_above_1200(self):
        # 1201 chars → long structured request
        prompt = "A" * 1201
        assert work_heuristic_reason(prompt) == "long structured request"

    def test_wh_13_at_1199(self):
        # 1199 chars → None (no triggers)
        prompt = "A" * 1199
        assert work_heuristic_reason(prompt) is None

    def test_wh_14_create_app(self):
        result = work_heuristic_reason("create an app that tracks expenses")
        assert result == "imperative tool-shaped request"

    def test_wh_15_fix_bug(self):
        result = work_heuristic_reason("fix the bug in my code")
        assert result == "imperative tool-shaped request"


class TestFallbackChatReason:
    """Golden outputs of fallback_chat_reason."""

    def test_fc_01_hi_greeting(self):
        assert fallback_chat_reason("hi") == "greeting or smalltalk"

    def test_fc_02_hello_greeting(self):
        assert fallback_chat_reason("hello there") == "greeting or smalltalk"

    def test_fc_03_thanks_greeting(self):
        assert fallback_chat_reason("thanks!") == "greeting or smalltalk"

    def test_fc_04_question_mark(self):
        # Short prompt ending with ? → plain question
        assert fallback_chat_reason("What time is it?") == "plain question"

    def test_fc_05_what_start(self):
        assert fallback_chat_reason("What is the weather like?") == "plain question"

    def test_fc_06_explain_start(self):
        result = fallback_chat_reason("Explain how photosynthesis works.")
        assert result == "explanation request"

    def test_fc_07_how_start(self):
        result = fallback_chat_reason("How does gravity work?")
        assert result == "plain question"

    def test_fc_08_long_prompt_none(self):
        # > 500 chars → None (too long for fallback chat)
        prompt = "Tell me more about " + "stuff " * 90
        assert fallback_chat_reason(prompt) is None

    def test_fc_09_greeting_too_long(self):
        # greeting keyword but > 80 chars → None
        prompt = "hi " + "there " * 15  # > 80 chars
        assert fallback_chat_reason(prompt) is None

    def test_fc_10_exactly_80_chars_greeting(self):
        # Exactly 80 chars starting with hi → "greeting or smalltalk"
        prompt = ("hi " + "x" * 77)[:80]
        assert len(prompt) == 80
        assert fallback_chat_reason(prompt) == "greeting or smalltalk"

    def test_fc_11_at_81_chars_greeting_none(self):
        # 81 chars starting with hi → None (> 80)
        prompt = "hi " + "x" * 78
        assert len(prompt) == 81
        assert fallback_chat_reason(prompt) is None

    def test_fc_12_ambiguous_none(self):
        # Short, not a question, not a greeting, not an explanation
        result = fallback_chat_reason("blue")
        assert result is None

    def test_fc_13_describe_start(self):
        result = fallback_chat_reason("Describe the solar system.")
        assert result == "explanation request"

    def test_fc_14_exactly_500_chars_question(self):
        # Exactly 500 chars ending with ? → plain question
        prompt = ("What is " + "this " * 100)[:499] + "?"
        assert len(prompt) == 500
        assert fallback_chat_reason(prompt) == "plain question"

    def test_fc_15_at_501_chars_question_none(self):
        # 501 chars ending with ? → None (> 500)
        prompt = ("What is " + "this " * 99)[:500] + "?"
        assert len(prompt) == 501
        assert fallback_chat_reason(prompt) is None


# ---------------------------------------------------------------------------
# Regression: the two step counts are DIFFERENT measurements (lead fix)
#
# intent.work_heuristic_reason has always used a STRICT numbered-list
# pattern (1–2 digits + ". )" + whitespace + content).  model_routing
# has always used a LOOSE pattern (any digit count, bare markers, and
# "step N" phrases).  An early refactor draft fed intent the loose
# count, which would have widened intent's multi-step heuristic — a
# behaviour change.  These tests pin the original behaviour of both.
# ---------------------------------------------------------------------------

from aether_engine.routing.signals import text_signals


class TestStepCountsStayDistinct:
    def test_step_phrases_count_loose_only(self):
        sig = text_signals("step 1: mix the batter\nstep 2: pour it in")
        assert sig.step_count == 2
        assert sig.numbered_step_count == 0

    def test_step_phrases_do_not_trigger_intent_multistep(self):
        # Pre-P1c behaviour: "step N" lines alone never made intent
        # call a prompt multi-step work.
        assert work_heuristic_reason("step 1: mix the batter\nstep 2: pour it in") is None

    def test_year_lines_are_not_numbered_steps(self):
        sig = text_signals("2026. Revenue grew\n2027. Costs fell")
        assert sig.numbered_step_count == 0

    def test_bare_markers_counts(self):
        sig = text_signals("1.\n2.")
        assert sig.step_count == 2
        # The strict pattern's \s+ crosses the newline and \S matches
        # the "2", so the ORIGINAL intent regex counts this as 1 —
        # a quirk of the pre-P1c behaviour, preserved exactly
        # (verified differentially against the HEAD regex).
        assert sig.numbered_step_count == 1

    def test_real_numbered_list_counts_both(self):
        sig = text_signals("1. Mix the batter\n2. Pour it in")
        assert sig.step_count == 2
        assert sig.numbered_step_count == 2

    def test_real_numbered_list_still_triggers_intent_multistep(self):
        assert work_heuristic_reason("1. Mix the batter\n2. Pour it in") == "multi-step request"
