"""Epic 1, E1-08 (engine side): session search + model-written titles.

Search matches titles and message content with a snippet. Titles are
generated only while the stored title is still an auto default, are
cleaned to a short single line, and never fail the task path.
"""
from unittest.mock import patch

import pytest

from aether_engine.memory.session_store import (
    add_message,
    create_session,
    get_session,
    search_sessions,
    update_session_title,
)
from aether_engine.providers.base import StreamChunk
from aether_engine.session_titles import (
    clean_title,
    generate_session_title,
    is_default_title,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_memory.db"
    with patch("aether_engine.memory.session_store.DB_PATH", test_db):
        yield test_db


def test_search_matches_content_and_title_with_snippet():
    s1 = create_session("Weekend plans")
    add_message(s1, "user", "help me debug the flaky parser test")
    add_message(s1, "assistant", "the parser fails on empty input")
    s2 = create_session("Groceries")
    add_message(s2, "user", "buy milk and eggs")

    by_content = search_sessions("flaky parser")
    assert [r["id"] for r in by_content] == [s1]
    assert "flaky parser" in by_content[0]["snippet"]

    by_title = search_sessions("groceries")
    assert [r["id"] for r in by_title] == [s2]
    assert by_title[0]["snippet"] == ""  # title-only match

    assert search_sessions("nonexistent-term") == []
    assert search_sessions("  ") == []


def test_rename_via_update_session_title_still_works():
    s1 = create_session("Old")
    assert update_session_title(s1, "New Name") is True
    assert get_session(s1)["title"] == "New Name"
    assert update_session_title("missing-id", "Nope") is False


def test_is_default_title_shapes():
    assert is_default_title("New Chat", "anything") is True
    assert is_default_title("", "anything") is True
    assert is_default_title(None, "anything") is True
    # Auto title = a cut-off prefix of the first prompt.
    assert is_default_title("help me debug the flaky par", "help me debug the flaky parser test") is True
    # A real (renamed or model-written) title is not a prompt prefix.
    assert is_default_title("Parser debugging session", "help me debug the flaky parser test") is False
    # E1-F15 alignment: the full first prompt as title IS an auto
    # title — the creation path stores short prompts verbatim — so it
    # qualifies for model re-titling. (This pin previously asserted
    # the opposite; the full-product audit rejected that rationale.)
    assert is_default_title("short prompt", "short prompt") is True
    # The long-prompt auto shape: 30-char prefix + "...".
    long_prompt = "help me debug the flaky parser test that only fails on Tuesdays"
    assert is_default_title(long_prompt[:30] + "...", long_prompt) is True


def test_clean_title_normalises_model_output():
    assert clean_title('"Fix the parser."\n') == "Fix the parser"
    assert clean_title("  Multiple   spaces  here ") == "Multiple spaces here"
    assert clean_title("") is None
    assert clean_title(None) is None
    assert len(clean_title("x" * 200)) == 60


class _TitleProvider:
    def __init__(self, text="", fail=False):
        self.text = text
        self.fail = fail

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if self.fail:
            raise RuntimeError("provider down")
        yield StreamChunk(text=self.text, finish_reason="stop")


@pytest.mark.asyncio
async def test_generate_title_uses_the_model_and_cleans_it():
    title = await generate_session_title(
        _TitleProvider('"Debugging the flaky parser"'), "help me debug...", "it fails on empty input")
    assert title == "Debugging the flaky parser"


@pytest.mark.asyncio
async def test_generate_title_failure_is_none_not_an_exception():
    assert await generate_session_title(_TitleProvider(fail=True), "p", "r") is None
    assert await generate_session_title(_TitleProvider(""), "p", "r") is None
