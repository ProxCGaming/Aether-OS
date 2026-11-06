"""Deterministic eval prompts and grading logic for Option C self-tests.

The graders are exact on purpose (E6-NEW-18): each check is
token-aware or anchored, never a raw substring search. Substring
grading passed responses that merely contained the answer's
characters — "someone"/"money"/"done" for the word "one", "3910"
for 391, "BANANAS" for BANANA, prose mentioning ``def is_even``
for working code — which made the suite's pass verdicts unsound.
The accident cases are pinned in tests/test_capability_evals.py.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SelfTest:
    name: str
    category: str  # "reasoning", "code", "chat", "instruction_following"
    prompt: str
    validator: Callable[[str], bool]
    description: str


def _check_math_391(response: str) -> bool:
    # Token-exact: 391 must appear as a standalone number. The raw
    # substring arm this used to have also passed "3910" and "1391".
    clean = re.sub(r"[^\d]", " ", response).split()
    return "391" in clean


def _check_python_code(response: str) -> bool:
    # The response must actually define the function and return a
    # parity test. Prose that merely mentions "def is_even" and
    # "return" no longer passes; a definition without a parity
    # expression (or without a return) does not either.
    has_def = re.search(
        r"^\s*def\s+is_even\s*\([^)]*\)\s*:", response, re.MULTILINE
    )
    has_return = re.search(r"\breturn\b", response)
    has_parity = re.search(r"(%\s*2|==\s*0|&\s*1|/\s*2)", response)
    return bool(has_def and has_return and has_parity)


def _check_banana(response: str) -> bool:
    # The prompt demands the single word and nothing else, so the
    # whole trimmed response must be exactly that word — "BANANAS"
    # or "BANANA!" are something else.
    return response.strip().upper() == "BANANA"


def _check_logic_sisters(response: str) -> bool:
    clean = re.sub(r"[^\d]", " ", response).split()
    # Sally has 1 sister (there are 2 sisters in total in the family).
    # "one" counts only as a whole word — as a substring it also
    # matched "someone", "money" and "done".
    return "1" in clean or re.search(r"\bone\b", response.lower()) is not None


SELF_TEST_SUITE: list[SelfTest] = [
    SelfTest(
        name="arithmetic_reasoning",
        category="reasoning",
        prompt="What is 17 multiplied by 23? Reply with ONLY the number.",
        validator=_check_math_391,
        description="Evaluates precise arithmetic calculation and formatting obedience",
    ),
    SelfTest(
        name="code_generation",
        category="code",
        prompt="Write a Python function def is_even(n): that returns True if n is even, False otherwise.",
        validator=_check_python_code,
        description="Evaluates Python syntax generation and function signature fidelity",
    ),
    SelfTest(
        name="instruction_following",
        category="chat",
        prompt="Respond with only the single word BANANA and nothing else.",
        validator=_check_banana,
        description="Evaluates strict negative constraint and exact text reproduction",
    ),
    SelfTest(
        name="logic_deduction",
        category="reasoning",
        prompt="Sally has 3 brothers. Each brother has 2 sisters. How many sisters does Sally have? Reply with just the number.",
        validator=_check_logic_sisters,
        description="Evaluates relational family logic deduction",
    ),
]


def grade_test(test: SelfTest, response_text: str) -> dict[str, Any]:
    """Grade an LLM response against a SelfTest specification."""
    passed = test.validator(response_text)
    return {
        "name": test.name,
        "category": test.category,
        "passed": passed,
        "response_preview": response_text[:120].strip(),
    }
