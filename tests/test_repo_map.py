"""Epic 8, E8-3.1: repo map for graph tasks.

The repo map is the ambient code layer for graph tasks (grill Q15/Q18,
Aider-style hybrid): tree-sitter signatures from the live working tree,
defs/refs graph, PageRank personalised to the task (files and
identifiers mentioned in the prompt rank first), rendered inside a
~1k-token budget. Fast chat NEVER receives a map.

Layers covered:
  * rag.repo_map — extraction (signatures only, no bodies), ranking,
    budget enforcement, .gitignore respect, freshness after an edit.
  * langgraph.nodes.supervisor — the map lands in the supervisor's
    prompt on the graph path; no workspace -> no map.
  * langgraph.fast_chat — the same prompt through fast chat contains
    zero repo-map tokens and never triggers a map build.
"""
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableConfig

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.providers.base import StreamChunk
from aether_engine.rag.repo_map import build_repo_map, estimate_tokens

# ---------------------------------------------------------------------------
# Fixture repos
# ---------------------------------------------------------------------------

ALPHA = '''\
def compute_total(items):
    # BODY_ONLY_MARKER_alpha
    return sum(items)


class Greeter:
    def greet(self, name):
        return "hi " + name
'''

BETA = '''\
from alpha import compute_total


def helper():
    return compute_total([1, 2])


def use_it():
    a = compute_total([3])
    b = compute_total([4])
    return helper() + a + b
'''

GAMMA = '''\
def orphan():
    return 42
'''

UTIL_JS = """\
export function formatName(user) {
  return user.first + " " + user.last;
}

export class Widget {
  render() {
    return formatName({ first: "a", last: "b" });
  }
}
"""


def _write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path):
    _write(tmp_path, "alpha.py", ALPHA)
    _write(tmp_path, "beta.py", BETA)
    _write(tmp_path, "gamma.py", GAMMA)
    return tmp_path


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extracts_signatures_without_bodies(repo):
    text = build_repo_map([str(repo)], prompt="")
    assert "alpha.py:" in text
    assert "def compute_total(items):" in text
    assert "class Greeter:" in text
    assert "def greet(self, name):" in text
    assert "def helper():" in text
    assert "def orphan():" in text
    # Signatures only: function bodies never enter the map.
    assert "BODY_ONLY_MARKER_alpha" not in text
    assert "return sum" not in text


def test_extracts_javascript_signatures(tmp_path):
    _write(tmp_path, "util.js", UTIL_JS)
    text = build_repo_map([str(tmp_path)], prompt="")
    assert "function formatName(user)" in text
    assert "class Widget" in text
    assert "render()" in text


def test_gitignore_excludes_files(tmp_path):
    _write(tmp_path, ".gitignore", "ignored.py\n")
    _write(tmp_path, "ignored.py", "def ignored_fn():\n    pass\n")
    _write(tmp_path, "kept.py", "def kept_fn():\n    pass\n")
    text = build_repo_map([str(tmp_path)], prompt="")
    assert "kept_fn" in text
    assert "ignored_fn" not in text


def test_empty_workspace_gives_empty_map(tmp_path):
    assert build_repo_map([str(tmp_path)], prompt="anything") == ""


# ---------------------------------------------------------------------------
# Ranking (PageRank personalised to the task)
# ---------------------------------------------------------------------------

def test_mentioned_file_ranks_first(repo):
    text = build_repo_map([str(repo)], prompt="please fix helper in beta.py")
    assert text.index("beta.py:") < text.index("alpha.py:")


def test_mentioned_identifier_boosts_defining_file(repo):
    text = build_repo_map([str(repo)], prompt="compute_total looks wrong")
    assert text.index("alpha.py:") < text.index("beta.py:")


def test_referenced_file_outranks_orphan_without_mentions(repo):
    # beta references alpha's compute_total three times; gamma is an
    # orphan. With no mentions at all, the graph alone ranks alpha
    # above gamma.
    text = build_repo_map([str(repo)], prompt="")
    assert text.index("alpha.py:") < text.index("gamma.py:")


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def test_map_stays_within_default_budget(repo):
    text = build_repo_map([str(repo)], prompt="")
    assert estimate_tokens(text) <= 1000


def test_map_stays_within_tight_budget(tmp_path):
    for i in range(12):
        body = "\n".join(
            f"def fn_{i}_{j}(arg{j}):\n    return arg{j}\n" for j in range(8)
        )
        _write(tmp_path, f"mod_{i}.py", body)
    text = build_repo_map([str(tmp_path)], prompt="", token_budget=60)
    assert text  # something still fits
    assert estimate_tokens(text) <= 60


def test_zero_budget_disables_map(repo):
    assert build_repo_map([str(repo)], prompt="", token_budget=0) == ""


def test_big_file_does_not_starve_other_files(tmp_path):
    # One huge file must not eat the whole budget: the map is an
    # index across files, so a small file still appears (with its
    # definitions capped per file, most-referenced first).
    big = "\n".join(f"def big_fn_{j}(x):\n    return x\n" for j in range(60))
    _write(tmp_path, "a_big.py", big)
    _write(tmp_path, "z_small.py", "def small_fn():\n    return 1\n")
    text = build_repo_map([str(tmp_path)], prompt="", token_budget=200)
    assert estimate_tokens(text) <= 200
    assert "z_small.py:" in text
    assert "def small_fn():" in text


def test_config_default_budget_is_1000():
    assert UserConfig().repo_map_token_budget == 1000


# ---------------------------------------------------------------------------
# Freshness: rebuilt from live files, no manual invalidation
# ---------------------------------------------------------------------------

def test_map_updates_after_file_edit(repo):
    before = build_repo_map([str(repo)], prompt="")
    assert "brand_new_fn" not in before
    gamma = repo / "gamma.py"
    gamma.write_text(GAMMA + "\n\ndef brand_new_fn():\n    return 7\n", encoding="utf-8")
    after = build_repo_map([str(repo)], prompt="")
    assert "def brand_new_fn():" in after


# ---------------------------------------------------------------------------
# Graph-path injection (supervisor)
# ---------------------------------------------------------------------------

class InspectProvider:
    """Records the messages the supervisor shows the model."""

    def __init__(self):
        self.received_messages = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.received_messages.extend(messages)
        yield StreamChunk(
            text='{"next": "planner", "reason": "test", "briefing": ""}',
            finish_reason="stop",
        )


def _supervisor_state(prompt):
    return {
        "original_prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "session_id": "test-session",
        "task_id": "test-task",
        "plan": "",
    }


@pytest.mark.asyncio
async def test_supervisor_prompt_contains_repo_map(repo):
    provider = InspectProvider()
    config = RunnableConfig(configurable={
        "session_id": "test-session",
        "provider": provider,
        "workspace_roots": [str(repo)],
        "user_config": UserConfig(),
    })
    await supervisor_node(_supervisor_state("fix compute_total"), config)
    shown = "\n".join(str(m.get("content", "")) for m in provider.received_messages)
    assert "Repository map" in shown
    assert "def compute_total(items):" in shown


def test_routing_prompt_contains_repo_map(repo):
    """The live create_supervisor routing prompt (supervisor_model)
    carries the same map section as supervisor_node."""
    from langgraph.config import var_child_runnable_config

    from aether_engine.langgraph.supervisor_model import supervisor_routing_prompt

    state = {
        "original_prompt": "fix compute_total",
        "plan": "",
        "messages": [{"role": "user", "content": "fix compute_total"}],
    }
    config = {"configurable": {
        "workspace_roots": [str(repo)],
        "user_config": UserConfig(),
    }}
    token = var_child_runnable_config.set(config)
    try:
        messages = supervisor_routing_prompt(state)
    finally:
        var_child_runnable_config.reset(token)
    assert "Repository map" in messages[0].content
    assert "def compute_total(items):" in messages[0].content


@pytest.mark.asyncio
async def test_supervisor_without_workspace_has_no_map():
    provider = InspectProvider()
    config = RunnableConfig(configurable={
        "session_id": "test-session",
        "provider": provider,
        "user_config": UserConfig(),
    })
    await supervisor_node(_supervisor_state("hello"), config)
    shown = "\n".join(str(m.get("content", "")) for m in provider.received_messages)
    assert "Repository map" not in shown


# ---------------------------------------------------------------------------
# Fast chat exclusion (Q18: fast chat gets NO repo map, ever)
# ---------------------------------------------------------------------------

class ChunkProvider:
    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        yield StreamChunk(text="chat answer", finish_reason="stop")


@pytest.mark.asyncio
async def test_fast_chat_contains_no_repo_map(repo):
    provider = ChunkProvider()
    with patch(
        "aether_engine.rag.repo_map.build_repo_map",
        side_effect=AssertionError("fast chat must never build a repo map"),
    ):
        events = [
            event
            async for event in run_fast_chat(
                "fix compute_total in alpha.py", provider, session_id=None
            )
        ]
    assert events[-1].type == EventType.TASK_COMPLETED
    shown = "\n".join(
        str(m.get("content", "")) for call in provider.calls for m in call
    )
    assert "Repository map" not in shown
    assert "def compute_total" not in shown
