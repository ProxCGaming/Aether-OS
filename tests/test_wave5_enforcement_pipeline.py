"""Wave 5 registry split, step 2: Steps 1–2 of ``ToolRegistry.execute``
live in ``aether_engine.tools.enforcement.EnforcementPipeline``.

The S2-M1/FUP-1 suites pin the re-check's BEHAVIOUR (rewritten args
are re-vetted, approval binding, error shapes, audit events). What
they cannot pin is the structural property this extraction exists
to create: the initial check and the Step 2.55 re-check are not two
copies of the enforcement logic that could drift apart — they are
two calls to ``enforce`` on ONE pipeline object built for the call.
These tests pin exactly that, by spying on the pipeline class at
its home module (``execute`` resolves it there at call time):

* one pipeline instance per ``execute`` call;
* the initial pass and (when a hook rewrites the args) the re-check
  go through that same instance, in that order, policy step before
  pre-flight step within each pass;
* unchanged args produce exactly one pass (no double-audit re-run);
* the re-check's ``honor_approval`` flag carries the FUP-1 binding
  decision (``approval_binding="carry"`` → True, default "rebind"
  → False).
"""

from __future__ import annotations

import pytest

from aether_engine.tools import enforcement, hooks
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk


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


class _PipelineSpy:
    """Records every pipeline construction and step call, delegating
    to the real methods so behaviour is the production behaviour."""

    def __init__(self, monkeypatch):
        self.instances = []
        self.calls = []  # (instance, step, args, honor_approval)
        real_policy = enforcement.EnforcementPipeline.enforce_policy
        real_preflight = enforcement.EnforcementPipeline.enforce_preflight
        spy = self

        def enforce_policy(instance, check_args, *, honor_approval=True):
            spy.calls.append((instance, "policy", dict(check_args), honor_approval))
            return real_policy(instance, check_args, honor_approval=honor_approval)

        def enforce_preflight(instance, check_args):
            spy.calls.append((instance, "preflight", dict(check_args), None))
            return real_preflight(instance, check_args)

        real_init = enforcement.EnforcementPipeline.__init__

        def init(instance, **kwargs):
            spy.instances.append(instance)
            return real_init(instance, **kwargs)

        monkeypatch.setattr(enforcement.EnforcementPipeline, "__init__", init)
        monkeypatch.setattr(
            enforcement.EnforcementPipeline, "enforce_policy", enforce_policy)
        monkeypatch.setattr(
            enforcement.EnforcementPipeline, "enforce_preflight", enforce_preflight)


def _read_registry(ran: list) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file",
        description="x",
        execute_fn=lambda path: ran.append(path) or "body",
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


def _rewrite_hook(new_args):
    def _hook(tool_name, args, _context):
        return dict(new_args)

    return _hook


async def test_initial_and_recheck_share_one_pipeline_instance(monkeypatch):
    spy = _PipelineSpy(monkeypatch)
    ran = []
    registry = _read_registry(ran)
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"path": "/tmp/rewritten.txt"}))

    result = await registry.execute(
        "read_file", {"path": "/tmp/original.txt"},
        workspace_roots=["/tmp"], audit_logger=_Audit())

    assert result == "body"
    assert ran == ["/tmp/rewritten.txt"]
    # Exactly one pipeline object was built for this execute() call,
    # and both passes ran on it: policy->preflight on the proposed
    # args, then policy->preflight on the rewritten args.
    assert len(spy.instances) == 1
    instance = spy.instances[0]
    assert [c[0] for c in spy.calls] == [instance] * 4
    assert [c[1] for c in spy.calls] == [
        "policy", "preflight", "policy", "preflight"]
    assert spy.calls[0][2] == {"path": "/tmp/original.txt"}
    assert spy.calls[2][2] == {"path": "/tmp/rewritten.txt"}
    # Default approval_binding="rebind": the re-check must not honour
    # the per-call approval flag (this call was unapproved, so the
    # flag is False purely from the binding decision).
    assert spy.calls[0][3] is True
    assert spy.calls[2][3] is False


async def test_carry_binding_forwards_honor_approval_true(monkeypatch):
    spy = _PipelineSpy(monkeypatch)
    ran = []
    registry = _read_registry(ran)
    hooks.register_tool_call_hook(
        "retarget", _rewrite_hook({"path": "/tmp/rewritten.txt"}))

    await registry.execute(
        "read_file", {"path": "/tmp/original.txt"},
        workspace_roots=["/tmp"], approved=True,
        approval_binding="carry", audit_logger=_Audit())

    assert len(spy.instances) == 1
    assert spy.calls[2][3] is True


async def test_unchanged_args_run_exactly_one_pass(monkeypatch):
    spy = _PipelineSpy(monkeypatch)
    ran = []
    registry = _read_registry(ran)

    result = await registry.execute(
        "read_file", {"path": "/tmp/original.txt"},
        workspace_roots=["/tmp"], audit_logger=_Audit())

    assert result == "body"
    # No hook rewrite -> no Step 2.55 re-run (re-running would only
    # double-audit); a single policy->preflight pass on one instance.
    assert len(spy.instances) == 1
    assert [c[1] for c in spy.calls] == ["policy", "preflight"]
