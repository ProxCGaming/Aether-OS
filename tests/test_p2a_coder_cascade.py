"""P2a — the coder verifier cascade: verifiers, the ladder loop,
the coder-node gates, and the Task Ledger tap.

No live model is called anywhere in this file: backends are
scripted stubs behind the shared CoderBackend shape, transcripts
are JSON files written into tmp dirs, and the coder node's ladder
construction is substituted at its own seam.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.backends.coder_backend import (
    CoderBudget,
    CoderTaskResult,
)
from aether_engine.langgraph.coder_cascade import (
    MAX_ATTEMPTS,
    CascadeRung,
    cascade_summary_line,
    run_cascade,
    verify_attempt,
)
from aether_engine.langgraph.coder_cascade import (
    test_verdict_from_transcript as _transcript_verdict,
)

# ---------------------------------------------------------------------------
# Fixtures: results, transcripts, stub backends, rungs
# ---------------------------------------------------------------------------

DIFF = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-return a - b\n+return a + b\n"


def _result(**over) -> CoderTaskResult:
    base = {
        "status": "done", "diff": DIFF, "summary": "Fixed add()",
        "files_touched": ["hello.py"], "cost_actual": 0.25,
        "transcript_ref": "", "backend": "miniswe", "tokens_actual": 100}
    base.update(over)
    return CoderTaskResult(**base)


def _transcript(tmp_path: Path, *contents: str) -> str:
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({
        "backend": "miniswe", "complete": True,
        "messages": [{"role": "user", "content": c} for c in contents],
    }), encoding="utf-8")
    return str(path)


class _StubBackend:
    name = "miniswe"

    def __init__(self, results):
        self._results = list(results)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self._results.pop(0)


def _rung(tier: int, tier_name: str, backend: _StubBackend) -> CascadeRung:
    return CascadeRung(
        tier=tier, tier_name=tier_name,
        model_alias=f"prov/{tier_name}-model",
        provider_name="prov", model=f"{tier_name}-model",
        backend_name="miniswe", run=backend.run)


# ---------------------------------------------------------------------------
# V1 — the schema verdict on the result shape
# ---------------------------------------------------------------------------


class TestSchemaVerifier:
    def test_done_with_diff_and_no_tests_recorded_is_accepted(self, tmp_path):
        verdict = verify_attempt(_result(transcript_ref=str(tmp_path / "none.json")))
        assert verdict.accepted
        assert verdict.verifier == "schema"

    def test_failed_status_is_a_rejection(self):
        verdict = verify_attempt(_result(status="failed", summary="kaboom"))
        assert not verdict.accepted
        assert verdict.verifier == "schema"
        assert "kaboom" in verdict.reason

    def test_budget_stopped_is_a_rejection(self):
        verdict = verify_attempt(_result(status="budget-stopped", summary="cost limit"))
        assert not verdict.accepted
        assert "budget-stopped" in verdict.reason

    def test_done_with_empty_diff_is_a_rejection(self):
        verdict = verify_attempt(_result(diff="", files_touched=[]))
        assert not verdict.accepted
        assert "no work product" in verdict.reason or "empty diff" in verdict.reason

    def test_diff_without_files_is_a_rejection(self):
        verdict = verify_attempt(_result(files_touched=[]))
        assert not verdict.accepted
        assert verdict.verifier == "schema"


# ---------------------------------------------------------------------------
# V2 — tests read from the run's own transcript, never re-run
# ---------------------------------------------------------------------------


class TestTranscriptVerifier:
    def test_pytest_pass(self, tmp_path):
        ref = _transcript(tmp_path, "===== 12 passed in 0.42s =====")
        assert _transcript_verdict(ref) == ("pass", None)

    def test_pytest_failure_names_the_failed_tests(self, tmp_path):
        ref = _transcript(
            tmp_path,
            "FAILED tests/test_add.py::test_sign - AssertionError\n"
            "===== 1 failed, 11 passed in 0.42s =====")
        verdict, reason = _transcript_verdict(ref)
        assert verdict == "fail"
        assert "1 failed" in reason
        assert "tests/test_add.py::test_sign" in reason

    def test_jest_failure(self, tmp_path):
        ref = _transcript(tmp_path, "Tests: 2 failed, 10 passed, 12 total")
        verdict, reason = _transcript_verdict(ref)
        assert verdict == "fail"
        assert "2 failed" in reason

    def test_last_recorded_run_binds(self, tmp_path):
        ref = _transcript(
            tmp_path,
            "===== 3 failed, 2 passed in 0.10s =====",
            "fixed the failures, re-running",
            "===== 5 passed in 0.09s =====")
        assert _transcript_verdict(ref) == ("pass", None)

    def test_no_test_run_recorded_is_absent(self, tmp_path):
        ref = _transcript(tmp_path, "I edited hello.py and submitted.")
        assert _transcript_verdict(ref) == ("absent", None)

    def test_missing_transcript_is_absent_not_a_failure(self, tmp_path):
        assert _transcript_verdict(
            str(tmp_path / "gone.json")) == ("absent", None)
        assert _transcript_verdict("") == ("absent", None)

    def test_v2_rejection_beats_a_valid_shape(self, tmp_path):
        ref = _transcript(tmp_path, "===== 2 failed, 1 passed in 0.1s =====")
        verdict = verify_attempt(_result(transcript_ref=ref))
        assert not verdict.accepted
        assert verdict.verifier == "tests"


# ---------------------------------------------------------------------------
# The ladder loop
# ---------------------------------------------------------------------------


class TestCascadeLoop:
    def test_accepted_cheap_attempt_never_escalates(self):
        cheap = _StubBackend([_result()])
        decided = _StubBackend([_result()])
        outcome = run_cascade(
            rungs=[_rung(0, "small", cheap), _rung(1, "standard", decided)],
            task_briefing="Fix it", repo_path="/repo")
        assert outcome.accepted
        assert len(outcome.attempts) == 1
        assert decided.requests == []
        assert cheap.requests[0].attempt_context is None

    def test_rejection_escalates_with_context_verbatim(self, tmp_path):
        ref = _transcript(tmp_path, "===== 1 failed, 3 passed in 0.1s =====")
        cheap = _StubBackend([_result(transcript_ref=ref)])
        decided = _StubBackend([_result(summary="Fixed properly")])
        outcome = run_cascade(
            rungs=[_rung(0, "small", cheap), _rung(1, "standard", decided)],
            task_briefing="Fix it", repo_path="/repo")
        assert outcome.accepted
        assert len(outcome.attempts) == 2
        first, second = outcome.attempts
        assert first["verdict"] == "rejected" and first["verifier"] == "tests"
        assert second["verdict"] == "accepted"
        context = decided.requests[0].attempt_context
        assert context is not None
        assert context.prior_diff == DIFF
        assert context.rejection_reason == first["reason"]
        assert "1 failed" in context.rejection_reason
        assert outcome.summary_line == (
            "coder: small → rejected (tests) → standard ✓")

    def test_default_ladder_stops_at_two_and_surfaces_the_rejection(self):
        cheap = _StubBackend([_result(status="failed", summary="boom")])
        decided = _StubBackend([_result(status="failed", summary="boom again")])
        strong = _StubBackend([_result()])
        outcome = run_cascade(
            rungs=[_rung(0, "small", cheap), _rung(1, "standard", decided),
                   _rung(2, "strong", strong)],
            task_briefing="Fix it", repo_path="/repo")
        assert not outcome.accepted
        assert len(outcome.attempts) == 2  # two-step ladder is off
        assert strong.requests == []
        assert outcome.final_rejection_reason
        assert "boom again" in outcome.final_rejection_reason
        # The last result stands, as the plan requires.
        assert outcome.result.summary == "boom again"

    def test_two_step_ladder_reaches_the_third_rung(self):
        cheap = _StubBackend([_result(status="failed", summary="boom")])
        decided = _StubBackend([_result(diff="")])
        strong = _StubBackend([_result(summary="Strong fix")])
        outcome = run_cascade(
            rungs=[_rung(0, "small", cheap), _rung(1, "standard", decided),
                   _rung(2, "strong", strong)],
            task_briefing="Fix it", repo_path="/repo", two_step=True)
        assert outcome.accepted
        assert len(outcome.attempts) == 3
        # The third attempt inherits the SECOND attempt's rejection.
        context = strong.requests[0].attempt_context
        assert context is not None and "empty diff" in context.rejection_reason

    def test_attempt_cap_binds_even_with_more_rungs(self):
        stubs = [_StubBackend([_result(status="failed", summary="x")])
                 for _ in range(5)]
        rungs = [_rung(i, f"t{i}", s) for i, s in enumerate(stubs)]
        outcome = run_cascade(
            rungs=rungs, task_briefing="Fix it", repo_path="/repo",
            two_step=True)
        assert len(outcome.attempts) == MAX_ATTEMPTS == 3

    def test_every_attempt_carries_the_callers_budget(self):
        budget = CoderBudget(tokens=10, usd=0.5, wall_clock_seconds=30.0)
        cheap = _StubBackend([_result(status="failed", summary="x")])
        decided = _StubBackend([_result()])
        run_cascade(
            rungs=[_rung(0, "small", cheap), _rung(1, "standard", decided)],
            task_briefing="Fix it", repo_path="/repo", budget=budget)
        assert cheap.requests[0].budget is budget
        assert decided.requests[0].budget is budget

    def test_summary_line_single_acceptance(self):
        assert cascade_summary_line(
            [{"tier_name": "small", "verdict": "accepted"}]) == "coder: small ✓"


# ---------------------------------------------------------------------------
# The coder node: gates + the S22 contract
# ---------------------------------------------------------------------------


def _state(**over) -> dict:
    state = {
        "messages": [{"role": "user", "content": "Fix the sign bug"}],
        "task_id": "task-p2a",
        "plan_mode": False,
        "plan": "",
        "context_briefing": "",
        "original_prompt": "Fix the sign bug",
    }
    state.update(over)
    return state


def _config(user_config, **configurable) -> dict:
    base = {"provider": object(), "user_config": user_config,
            "workspace_roots": ["/tmp/some-repo"]}
    base.update(configurable)
    return {"configurable": base}


def _cascade_config(**over) -> UserConfig:
    base = {"coder_backend": "miniswe", "coder_cascade": True,
            "model_routing": "auto"}
    base.update(over)
    return UserConfig(**base)


class TestCoderNodeCascadeGates:
    @pytest.mark.asyncio
    async def test_flag_off_is_the_single_run(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend([_result()])
        monkeypatch.setattr(
            coder, "_build_miniswe_backend",
            lambda config, provider: (stub, "prov/standard-model"))
        monkeypatch.setattr(coder, "save_artifact", lambda *a: "/a.txt")
        out = await coder.coder_node(
            _state(), _config(_cascade_config(coder_cascade=False)))
        assert len(stub.requests) == 1
        entry = out["delegation_log"][0]
        assert "cascade_attempt" not in entry

    @pytest.mark.asyncio
    async def test_routing_off_is_the_single_run(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend([_result()])
        monkeypatch.setattr(
            coder, "_build_miniswe_backend",
            lambda config, provider: (stub, "prov/standard-model"))
        monkeypatch.setattr(coder, "save_artifact", lambda *a: "/a.txt")
        out = await coder.coder_node(
            _state(), _config(_cascade_config(model_routing="off")))
        assert len(stub.requests) == 1
        assert "cascade_attempt" not in out["delegation_log"][0]

    @pytest.mark.asyncio
    async def test_a_pin_is_never_cascaded(self, monkeypatch):
        from aether_engine.langgraph.nodes import coder

        stub = _StubBackend([_result()])
        monkeypatch.setattr(
            coder, "_build_miniswe_backend",
            lambda config, provider: (stub, "prov/standard-model"))
        monkeypatch.setattr(coder, "save_artifact", lambda *a: "/a.txt")
        out = await coder.coder_node(
            _state(), _config(
                _cascade_config(),
                agent_models={"coder": "prov:pinned-model"}))
        assert len(stub.requests) == 1
        assert "cascade_attempt" not in out["delegation_log"][0]


class TestS22Contract:
    """S22: a coder delegation whose small-tier attempt fails its
    tests is retried at the decided tier, and the delegation log
    shows both attempts with the cascade reason."""

    @pytest.mark.asyncio
    async def test_s22_rejected_small_attempt_retried_at_decided_tier(
            self, monkeypatch, tmp_path):
        from aether_engine.langgraph import coder_cascade
        from aether_engine.langgraph.nodes import coder

        ref = _transcript(
            tmp_path, "FAILED tests/test_add.py::test_sign\n"
                      "===== 1 failed, 3 passed in 0.1s =====")
        cheap = _StubBackend([_result(transcript_ref=ref)])
        decided = _StubBackend([_result(summary="Fixed properly")])
        ladder = [_rung(0, "small", cheap), _rung(1, "standard", decided)]
        monkeypatch.setattr(coder, "_cascade_ladder", lambda *a: ladder)
        monkeypatch.setattr(coder, "save_artifact", lambda *a: "/a.txt")
        monkeypatch.setattr(
            coder_cascade, "publish_cascade_activity", lambda *a: None)
        monkeypatch.setattr(
            "aether_engine.langgraph.model_routing.record_model_outcome",
            lambda *a: None)

        out = await coder.coder_node(_state(), _config(_cascade_config()))

        entries = out["delegation_log"]
        assert len(entries) == 2
        assert entries[0]["cascade_tier"] == "small"
        assert entries[0]["cascade_verdict"] == "rejected"
        assert entries[0]["cascade_verifier"] == "tests"
        assert "1 failed" in entries[0]["cascade_reason"]
        assert entries[1]["cascade_tier"] == "standard"
        assert entries[1]["cascade_verdict"] == "accepted"
        # The retry was briefed with the rejection + the prior diff.
        context = decided.requests[0].attempt_context
        assert context is not None
        assert context.rejection_reason == entries[0]["cascade_reason"]
        assert context.prior_diff == DIFF
        content = out["messages"][0]["content"]
        assert "Fixed properly" in content
        assert "coder: small → rejected (tests) → standard ✓" in content

    @pytest.mark.asyncio
    async def test_exhausted_ladder_surfaces_the_rejection(
            self, monkeypatch):
        from aether_engine.langgraph import coder_cascade
        from aether_engine.langgraph.nodes import coder

        cheap = _StubBackend([_result(status="failed", summary="boom")])
        decided = _StubBackend(
            [_result(status="failed", summary="boom again")])
        ladder = [_rung(0, "small", cheap), _rung(1, "standard", decided)]
        monkeypatch.setattr(coder, "_cascade_ladder", lambda *a: ladder)
        monkeypatch.setattr(coder, "save_artifact", lambda *a: "/a.txt")
        monkeypatch.setattr(
            coder_cascade, "publish_cascade_activity", lambda *a: None)
        monkeypatch.setattr(
            "aether_engine.langgraph.model_routing.record_model_outcome",
            lambda *a: None)

        out = await coder.coder_node(_state(), _config(_cascade_config()))
        content = out["messages"][0]["content"]
        assert "final attempt was rejected" in content
        assert "boom again" in content


# ---------------------------------------------------------------------------
# Config + the Task Ledger tap
# ---------------------------------------------------------------------------


class TestConfigAndLedger:
    def test_config_flags_default_off_and_round_trip(self):
        assert UserConfig().coder_cascade is False
        assert UserConfig().coder_cascade_two_step is False
        loaded = UserConfig.from_dict(
            {"coder_cascade": True, "coder_cascade_two_step": True})
        assert loaded.coder_cascade is True
        assert loaded.coder_cascade_two_step is True
        assert UserConfig(coder_cascade=True).to_dict()[
            "coder_cascade"] is True

    def test_ledger_tap_records_cascade_attempts(self):
        from aether_common.contracts import Event, EventType
        from aether_engine import task_ledger

        task_id = "task-p2a-ledger"
        for attempt, verdict, verifier in (
                (1, "rejected", "tests"), (2, "accepted", "tests")):
            task_ledger.record_executor_event(task_id, Event(
                type=EventType.NODE_ACTIVITY, request_id=task_id,
                payload={
                    "task_id": task_id, "from_node": "coder",
                    "to_node": "coder", "action": "model_cascade",
                    "reason": "1 failed" if verdict == "rejected" else "",
                    "output_payload": {
                        "attempt": attempt, "tier": "small"
                        if attempt == 1 else "standard",
                        "model": "prov/m", "backend": "miniswe",
                        "verifier": verifier, "verdict": verdict,
                        "reason": "1 failed" if verdict == "rejected" else "",
                        "status": "done"},
                    "status": "completed", "timestamp": 1}))
        entry = task_ledger._TAP[task_id]
        assert len(entry["model_cascade"]) == 2
        assert task_ledger._cascade_summary(entry["model_cascade"]) == (
            "coder: small → rejected (tests) → standard ✓")
