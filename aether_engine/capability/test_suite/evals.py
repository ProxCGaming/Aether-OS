"""Deterministic eval prompts and grading logic for Option C self-tests."""
from dataclasses import dataclass
import re
from typing import Any, Callable, Dict, List, Optional


@dataclass
class SelfTest:
    name: str
    category: str  # "reasoning", "code", "chat", "instruction_following"
    prompt: str
    validator: Callable[[str], bool]
    description: str


def _check_math_391(response: str) -> bool:
    clean = re.sub(r"[^\d]", " ", response).split()
    return "391" in clean or "391" in response


def _check_python_code(response: str) -> bool:
    lower = response.lower()
    return ("def is_even" in lower or "def is_even(" in response) and ("return" in lower or "% 2" in lower)


def _check_banana(response: str) -> bool:
    return "BANANA" in response.strip().upper()


def _check_logic_sisters(response: str) -> bool:
    clean = re.sub(r"[^\d]", " ", response).split()
    # Sally has 1 sister (there are 2 sisters in total in the family)
    return "1" in clean or "one" in response.lower()


SELF_TEST_SUITE: List[SelfTest] = [
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


def grade_test(test: SelfTest, response_text: str) -> Dict[str, Any]:
    """Grade an LLM response against a SelfTest specification."""
    passed = test.validator(response_text)
    return {
        "name": test.name,
        "category": test.category,
        "passed": passed,
        "response_preview": response_text[:120].strip(),
    }
