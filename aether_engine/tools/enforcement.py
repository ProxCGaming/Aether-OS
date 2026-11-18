"""Tool enforcement pipeline — Steps 1–2 of ``ToolRegistry.execute``.

Wave 5 registry split, step 2: the policy check (Step 1, ADR 0021)
and the pre-flight validation (Step 2, ADR 0009 §2) used to live in
two closures *inside* ``ToolRegistry.execute`` so the post-hook
re-check (Step 2.55, S2-M1) could re-run them on hook-rewritten
arguments. They live here as one named pipeline instead, so that
property is structural rather than aspirational: ``execute`` builds
exactly one :class:`EnforcementPipeline` per call, and BOTH the
initial check and the Step 2.55 re-check call ``enforce`` on that
same object — the re-check re-runs EXACTLY the same enforcement —
same verdicts, same audit events, same error shapes — on the
rewritten arguments, never a divergent copy of it.

One instance carries one call's context (tool, registry, policy
inputs, approval state, audit logger); it is not shared across
calls. The pipeline is synchronous and raises
:class:`~aether_engine.tools.registry.ToolExecutionError` on any
refusal, exactly as the closures did.

Imports of the policy / validation functions and of
``ToolExecutionError`` are deferred to call time, as they were
inside ``execute``: ``tools.policy`` imports ``Tool``/``ToolRisk``
from ``tools.registry`` and this module is imported by
``tools.registry``, so a top-level import would be circular; the
deferred form also keeps the established patch surface intact
(tests patch ``aether_engine.validation.pre_flight.validate_tool_request``
at its home module and the pipeline must run the patched function).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aether_engine.tools.registry import Tool, ToolRegistry

logger = logging.getLogger("aether_engine.tools.enforcement")


class EnforcementPipeline:
    """Steps 1–2 (policy + pre-flight) for one tool call.

    ``enforce`` runs the two steps in pipeline order — policy first,
    pre-flight second — and is the single entry point both the
    initial check in ``ToolRegistry.execute`` and its Step 2.55
    post-hook re-check call.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        tool: Tool,
        registry: ToolRegistry,
        audit_logger: Any,
        policies: dict[str, str] | None = None,
        disabled_tools: Any = None,
        path_rules: list | None = None,
        shell_extras: dict | None = None,
        approval_mode: str = "ask",
        approved: bool = False,
        approval_cache: Any = None,
        workspace_roots: list | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.tool = tool
        self.registry = registry
        self.log = audit_logger
        self.policies = policies
        self.disabled_tools = disabled_tools
        self.path_rules = path_rules
        self.shell_extras = shell_extras
        self.approval_mode = approval_mode
        self.approved = approved
        self.approval_cache = approval_cache
        self.workspace_roots = workspace_roots

    def enforce(self, check_args: dict[str, Any], *, honor_approval: bool = True) -> None:
        """Run Steps 1–2, in order, on ``check_args``.

        ``honor_approval`` is forwarded to the policy step only:
        Step 2.55's "rebind" re-check (FUP-1) passes ``False`` so the
        per-call approval given for the pre-hook arguments does not
        satisfy an "ask" verdict on the final arguments; pre-flight
        has no approval notion and is identical in both passes.
        """
        self.enforce_policy(check_args, honor_approval=honor_approval)
        self.enforce_preflight(check_args)

    # ── Step 1: policy (ADR 0021; arg/path-aware since E2-06) ─────
    # F-03: policy resolution is part of the validation path (it
    # evaluates the shell safety rules for execute_shell), so an
    # internal error there must fail closed exactly like a
    # pre-flight error in Step 2 — denied, audited, and surfaced
    # as ToolExecutionError — never a raw exception escaping
    # execute() with no audit record of the refusal.
    def enforce_policy(self, check_args: dict[str, Any], *, honor_approval: bool = True) -> None:
        from aether_engine.tools.policy import resolve_policy_by_name, shell_denial_rule
        from aether_engine.tools.registry import ToolExecutionError

        name = self.tool_name
        tool = self.tool
        log = self.log
        # FUP-1: honor_approval=False is the Step 2.55 "rebind"
        # re-check — the per-call ``approved`` flag was consent
        # for the PRE-hook arguments and must not satisfy an
        # "ask" verdict on the final args. The approval_cache
        # covering-grant path below is deliberately NOT gated on
        # honor_approval: a stored grant is vetted against the
        # final args by covering_grant itself (prefix + risk
        # ceiling + the floored-path refusal), so it stays valid
        # in both binding modes, unchanged.
        effective_approved = self.approved and honor_approval
        try:
            policy = resolve_policy_by_name(name, self.policies or {}, self.registry, args=check_args, path_rules=self.path_rules, shell_extras=self.shell_extras, approval_mode=self.approval_mode, disabled_tools=self.disabled_tools)
        except Exception as e:
            try:
                log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": f"policy check failed internally: {e}", "via": "registry.execute"})
            except Exception:  # best-effort audit write
                logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
            raise ToolExecutionError(f"Pre-flight validation failed for '{name}': policy check failed internally: {e}") from e
        # E2-09: a stored approval grant turns this call's "ask" into
        # "allow" — never a "deny", never a floored path (the cache
        # itself refuses those), and the hit is audited with its grant.
        if policy == "ask" and not effective_approved and self.approval_cache is not None:
            try:
                grant = self.approval_cache.covering_grant(risk=tool.risk.value, args=check_args)
            except Exception:
                # PF-5: a cache failure must degrade to the normal
                # ask, never break execution — the same guard
                # tool_router (langgraph/graph.py) wraps around
                # its covering_grant check. The grant is treated
                # as absent, so the ordinary policy flow below
                # decides (ask → approval refusal, fail-closed).
                logger.debug("Approval-cache check failed in registry.execute", exc_info=True)
                grant = None
            if grant is not None:
                policy = "allow"
                try:
                    log.log_event("APPROVAL_CACHE_HIT", {"tool_name": name, "grant_id": grant["id"], "prefix": grant["prefix"]})
                except Exception:  # best-effort audit, as with TOOL_EXECUTED below
                    logger.debug("Failed to write APPROVAL_CACHE_HIT audit event", exc_info=True)
        if policy == "deny":
            payload = {"tool_name": name, "reason": "User Deny", "via": "registry.execute"}
            # E2-F13: when a shell safety rule produced the denial,
            # name the matched rule in the audit trail (the PRD
            # requires logging the matched pattern). A plain config
            # Deny matches no rule and keeps the bare payload.
            rule = shell_denial_rule(tool, check_args, self.shell_extras)
            if rule is not None:
                payload["rule"] = rule
            log.log_event("TOOL_BLOCKED_BY_POLICY", payload)
            raise ToolExecutionError(f"Tool '{name}' is blocked by policy (deny).")
        if policy == "ask" and not effective_approved:
            log.log_event("TOOL_BLOCKED_BY_POLICY", {"tool_name": name, "reason": "Bypassed Approval", "via": "registry.execute"})
            if not honor_approval and self.approved:
                # FUP-1 rebind: the call WAS approved — but for
                # the pre-hook arguments. Say exactly that, so
                # the fresh approval prompt is not a mystery.
                raise ToolExecutionError(
                    f"Tool '{name}' requires approval (policy=ask): "
                    "approval was for the pre-hook arguments, but a "
                    "hook rewrote the call and the final arguments "
                    "require approval (approval_binding=rebind). "
                    "Re-approval is required for the final version."
                )
            raise ToolExecutionError(
                f"Tool '{name}' requires approval (policy=ask). "
                "Pass approved=True only after explicit user consent."
            )

    # ── Step 2: pre-flight validation (ADR 0009 §2, fail-closed) ────
    # F-03: an internal error inside validation itself is a denial,
    # not an unwrapped escape: audit it as TOOL_VALIDATION_FAILED
    # and surface the same ToolExecutionError a validation *verdict*
    # produces below, so the error is logged/shown and no caller
    # can mistake the outcome for anything but a refusal.
    def enforce_preflight(self, check_args: dict[str, Any]) -> None:
        from aether_engine.tools.registry import ToolExecutionError
        from aether_engine.validation.pre_flight import validate_tool_request

        name = self.tool_name
        log = self.log
        try:
            validation = validate_tool_request(name, check_args, workspace_roots=self.workspace_roots)
        except Exception as e:
            try:
                log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": f"internal validation error: {e}", "via": "registry.execute"})
            except Exception:  # best-effort audit write
                logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
            raise ToolExecutionError(f"Pre-flight validation failed for '{name}': internal validation error: {e}") from e
        if not validation.is_valid:
            # E2-12: containment escapes and other validation failures
            # belong in the audit trail, not just the error string.
            try:
                log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": validation.error, "via": "registry.execute"})
            except Exception:  # best-effort audit write
                logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
            raise ToolExecutionError(f"Pre-flight validation failed for '{name}': {validation.error}")
