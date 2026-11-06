"""E6-NEW-18 (Wave 5): the Option C graders must be exact.

Each grader previously passed on substring accidents — responses
that merely contained the answer's characters. These tests pin both
directions: genuine answers still pass, and the accident cases from
the audit card now fail.
"""

import pytest

from aether_engine.capability.test_suite import SELF_TEST_SUITE, grade_test

_BY_NAME = {t.name: t for t in SELF_TEST_SUITE}


def _grade(name: str, response: str) -> bool:
    return grade_test(_BY_NAME[name], response)["passed"]


def test_suite_shape_is_unchanged():
    assert [t.name for t in SELF_TEST_SUITE] == [
        "arithmetic_reasoning",
        "code_generation",
        "instruction_following",
        "logic_deduction",
    ]


class TestMath391:
    @pytest.mark.parametrize(
        "response", ["391", "The answer is 391.", "**391**", "391\n"]
    )
    def test_real_answers_pass(self, response):
        assert _grade("arithmetic_reasoning", response) is True

    @pytest.mark.parametrize("response", ["3910", "1391", "390", "39", ""])
    def test_accidents_fail(self, response):
        # "3910"/"1391" passed pre-fix via the raw substring arm.
        assert _grade("arithmetic_reasoning", response) is False


class TestPythonCode:
    @pytest.mark.parametrize(
        "response",
        [
            "def is_even(n):\n    return n % 2 == 0",
            "def is_even(n):\n    if n % 2 == 0:\n        return True\n    return False",
            "```python\ndef is_even(n):\n    return n & 1 == 0\n```",
            "def is_even(n): return n % 2 == 0",
        ],
    )
    def test_real_code_passes(self, response):
        assert _grade("code_generation", response) is True

    @pytest.mark.parametrize(
        "response",
        [
            # Prose that mentions the pieces but defines nothing.
            "Use def is_even to check parity and return the result using % 2.",
            # A definition with no parity test.
            "def is_even(n):\n    return True",
            # A parity test with no return.
            "def is_even(n):\n    print(n % 2 == 0)",
            # Return + parity but no definition at all.
            "return n % 2 == 0",
        ],
    )
    def test_accidents_fail(self, response):
        assert _grade("code_generation", response) is False


class TestBanana:
    @pytest.mark.parametrize("response", ["BANANA", "banana", "  BANANA  \n"])
    def test_exact_word_passes(self, response):
        assert _grade("instruction_following", response) is True

    @pytest.mark.parametrize(
        "response", ["BANANAS", "BANANA!", "a BANANA", "BANANA BANANA", ""]
    )
    def test_accidents_fail(self, response):
        # The prompt demands the single word and nothing else;
        # "BANANAS" passed pre-fix via substring.
        assert _grade("instruction_following", response) is False


class TestLogicSisters:
    @pytest.mark.parametrize(
        "response", ["1", "one", "She has one sister.", "1 sister"]
    )
    def test_real_answers_pass(self, response):
        assert _grade("logic_deduction", response) is True

    @pytest.mark.parametrize(
        "response",
        ["someone", "money", "done", "none", "She has someone to ask", "2"],
    )
    def test_accidents_fail(self, response):
        # "someone"/"money"/"done"/"none" all contain "one" as a
        # substring and passed pre-fix.
        assert _grade("logic_deduction", response) is False
