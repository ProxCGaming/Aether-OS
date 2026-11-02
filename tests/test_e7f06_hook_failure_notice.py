"""E7-F06 — a throwing hook must not leak what it failed to vet.

The result-hook boundary used to build its failure notice by
interpolating the unrestricted ``str(exception)``:

    [tool output withheld: result hook '<name>' failed: {e}]

A parsing/validation hook's exception routinely embeds the very
result it failed to vet (a parse error quoting the payload), so the
"withheld" notice handed the unvetted output to the agent anyway.
The same interpolation existed in the tool_call fail-closed wrapper:
its HookBlock message flowed through the registry into the tool
message the agent reads.

Pins:
- the returned notice is a fixed form — hook name + the fact of
  failure — carrying no exception text, no result content, and no
  substring derived from either;
- the failure is still audited as HOOK_FAILED WITH the full error
  detail (the audit log is the operator's channel; the tool result
  is the model's channel);
- controls: a passing hook's replacement flows unchanged, a hook
  returning None keeps the result, hook-free results are untouched,
  and a hook's own deliberate HookBlock reason still surfaces.
"""

from __future__ import annotations

import asyncio

import pytest

from aether_engine.tools import hooks
from aether_engine.tools.hooks import HookBlock

# The audit's synthetic body, plus exception-only markers that appear
# nowhere in the tool result — they prove the *exception text itself*
# is gone from the notice, not just the embedded body.
BODY_MARKER = "SYNTHETIC_UNVETTED_PRIVATE_BODY_7"
RESULT_EXC_MARKER = "RESULT_HOOK_EXC_DETAIL_7"
CALL_EXC_MARKER = "CALL_HOOK_EXC_DETAIL_7"


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class _Audit:
    def __init__(self):
        self.events = []

    def log_event(self, name, payload):
        self.events.append((name, payload))

    def payloads(self, event):
        return [p for e, p in self.events if e == event]


def _throwing_result_hook(t, a, r, c):
    # The audit's shape: a parsing hook whose exception embeds the
    # result it was vetting, plus its own diagnostic detail.
    raise ValueError(f"could not parse payload {r} <{BODY_MARKER}> (detail {RESULT_EXC_MARKER})")


# ---------------------------------------------------------------------------
# tool_result hooks — the finding itself
# ---------------------------------------------------------------------------


def test_result_hook_failure_notice_is_fixed_form_unit():
    audit = _Audit()
    hooks.register_tool_result_hook("parser", _throwing_result_hook)
    out = hooks.apply_tool_result_hooks("read_file", {}, "the raw result", audit_logger=audit)
    assert out == "[tool output withheld: result hook 'parser' failed]"
    assert BODY_MARKER not in out
    assert RESULT_EXC_MARKER not in out
    assert "could not parse" not in out
    assert "the raw result" not in out
    # Diagnosability preserved on the operator's channel: the audit
    # event still records the failure WITH the exception detail.
    failed = audit.payloads("HOOK_FAILED")
    assert len(failed) == 1
    assert failed[0]["tool_name"] == "read_file"
    assert failed[0]["hook"] == "parser"
    assert RESULT_EXC_MARKER in failed[0]["error"]


def test_result_hook_failure_notice_through_real_registry():
    from aether_engine.tools.registry import build_tool_registry

    audit = _Audit()
    registry = build_tool_registry()
    hooks.register_tool_result_hook("parser", _throwing_result_hook)
    out = asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[], audit_logger=audit))
    assert isinstance(out, str)
    assert out == "[tool output withheld: result hook 'parser' failed]"
    assert BODY_MARKER not in out
    assert RESULT_EXC_MARKER not in out
    failed = audit.payloads("HOOK_FAILED")
    assert len(failed) == 1
    assert RESULT_EXC_MARKER in failed[0]["error"]


# ---------------------------------------------------------------------------
# tool_call hooks — the sibling interpolation in the same file
# ---------------------------------------------------------------------------


def test_call_hook_fail_closed_message_carries_no_exception_text_unit():
    audit = _Audit()

    def boom(t, a, c):
        raise RuntimeError(f"blew up on input (detail {CALL_EXC_MARKER})")

    hooks.register_tool_call_hook("guard", boom)
    with pytest.raises(HookBlock) as excinfo:
        hooks.apply_tool_call_hooks("read_file", {"path": "x"}, audit_logger=audit)
    msg = str(excinfo.value)
    assert CALL_EXC_MARKER not in msg
    assert "blew up" not in msg
    assert "guard" in msg
    assert "fails closed" in msg
    failed = audit.payloads("HOOK_FAILED")
    assert len(failed) == 1
    assert CALL_EXC_MARKER in failed[0]["error"]


def test_call_hook_fail_closed_message_through_real_registry():
    from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

    def boom(t, a, c):
        raise RuntimeError(f"blew up on input (detail {CALL_EXC_MARKER})")

    registry = build_tool_registry()
    hooks.register_tool_call_hook("guard", boom)
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[], audit_logger=_Audit()))
    assert CALL_EXC_MARKER not in str(excinfo.value)
    assert "blocked by a hook" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Controls — behaviour that must not change
# ---------------------------------------------------------------------------


def test_passing_result_hook_replacement_flows_unchanged():
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry()
    hooks.register_tool_result_hook("tag", lambda t, a, r, c: r + " [tagged]")
    out = asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[], audit_logger=_Audit()))
    assert out.endswith(" [tagged]")


def test_result_hook_returning_none_keeps_result():
    hooks.register_tool_result_hook("noop", lambda t, a, r, c: None)
    assert hooks.apply_tool_result_hooks("read_file", {}, "body") == "body"


def test_no_hooks_result_untouched():
    from aether_engine.tools.registry import build_tool_registry

    registry = build_tool_registry()
    out = asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[], audit_logger=_Audit()))
    assert isinstance(out, str) and out
    assert "withheld" not in out


def test_deliberate_hookblock_reason_still_surfaces():
    from aether_engine.tools.registry import ToolExecutionError, build_tool_registry

    registry = build_tool_registry()
    hooks.register_tool_call_hook(
        "no-time", lambda t, a, c: (_ for _ in ()).throw(HookBlock("no clock")))
    with pytest.raises(ToolExecutionError) as excinfo:
        asyncio.run(registry.execute("get_current_time", {}, workspace_roots=[], audit_logger=_Audit()))
    # A reason the hook author deliberately chose is their own
    # communication to the caller — not an interpolated exception —
    # and it must keep surfacing as today.
    assert "no clock" in str(excinfo.value)
