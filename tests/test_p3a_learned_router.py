"""Programme P3a: learned router — dataset, training gate, shadow mode.

The contract these tests pin:

* the dataset builder turns delegation_log history into the
  plan's labelled format — features from the P1c signals module,
  the serving model/tier joined from the task's routing records,
  the outcome joined from S14's outcome rows — and a delegation
  nothing joins stays UNLABELLED (counted, never guessed);
  denials are excluded and counted;
* the honesty gate is real: below 500 labelled delegations (the
  default) training refuses with an ``insufficient_data`` report
  and produces no artefact; the pipeline runs end-to-end only
  when the gate is met (or explicitly lowered for a fixture, with
  the threshold recorded in the report);
* the scorer answers from empirical tables only — cheapest tier
  whose record clears the bar, the evidence sentence from the raw
  counts, and NO opinion (None) where the record is thin;
* shadow mode never alters the served choice: with
  ``learned_routing = "shadow"`` and a trained artefact, the
  would-be choice is published with ``source: "learned-shadow"``
  alongside the heuristic's — and with the mode off (the
  default), no artefact, or a broken scorer, the run is exactly
  the S14 run and nothing is published;
* there is no "active" learned mode: config loads it as "off"
  and the Settings handoff validation refuses it.
"""
import pytest

from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.langgraph import learned_router, model_routing
from aether_engine.langgraph.learned_router import (
    DelegationRow,
    LearnedScorer,
    build_dataset,
    feature_bucket,
    length_band,
    load_scorer,
    run_training_pipeline,
    save_artefact,
    train_scorer,
)
from aether_engine.langgraph.model_routing import (
    History,
    ModelOutcomeStore,
    TaskSignals,
    collect_candidates,
    resolve_for_specialist,
    route,
)
from aether_engine.orchestration import core as orchestration_core
from aether_engine.routing.registry import ModelRegistry
from aether_engine.routing.signals import text_signals
from aether_engine.state import engine_state


# ── Fakes / helpers ───────────────────────────────────────────────


class FakeHealth:
    def is_provider_available(self, provider):
        return True


class FakeProvider:
    def __init__(self, provider_name, model):
        self.provider_name = provider_name
        self.model = model


class CapturingBus:
    def __init__(self):
        self.events = []

    def publish(self, task_id, event):
        self.events.append(event)


def _candidates(incumbent=("google_gemini", "gemini-3.5-flash")):
    return collect_candidates(
        registry=ModelRegistry(),
        configured_providers=["google_gemini"],
        health_manager=FakeHealth(),
        incumbent_provider=incumbent[0],
        incumbent_model=incumbent[1])


def _row(agent="coder", text="fix the typo", tier=1, success=True,
         ts=0.0, provider="google_gemini", model="some-model", verdict=None):
    sig = text_signals(text)
    return DelegationRow(
        agent=agent, task_text=text, wants_code=sig.wants_code,
        wants_reasoning=sig.wants_reasoning, step_count=sig.step_count,
        char_length=sig.char_length, length_band=length_band(sig.char_length),
        has_images=False, explicit_tier=None, provider=provider, model=model,
        served_tier=tier, success=success, verifier_verdict=verdict,
        cost_usd=None, latency_ms=None, escalated=False, timestamp=ts)


def _bucket(agent, text):
    sig = text_signals(text)
    return feature_bucket(
        agent, wants_code=sig.wants_code, wants_reasoning=sig.wants_reasoning,
        step_count=sig.step_count, char_length=sig.char_length)


def _cell(ok, bad, v_ok=0, v_bad=0):
    return {"ok": ok, "bad": bad, "v_ok": v_ok, "v_bad": v_bad}


@pytest.fixture(autouse=True)
def _clean_scorer_cache():
    learned_router.reset_scorer_cache()
    yield
    learned_router.reset_scorer_cache()


# ── Config ────────────────────────────────────────────────────────


def test_learned_routing_config_defaults_and_round_trip():
    cfg = UserConfig()
    assert cfg.learned_routing == "off"
    assert cfg.learned_router_artefact == ""
    assert UserConfig.from_dict(
        {"learned_routing": "shadow"}).learned_routing == "shadow"
    # No "active" mode exists: activation is Gate 5, and any other
    # value — including "active" — loads as "off".
    assert UserConfig.from_dict(
        {"learned_routing": "active"}).learned_routing == "off"
    assert UserConfig.from_dict(
        {"learned_routing": "banana"}).learned_routing == "off"
    assert UserConfig.from_dict({}).learned_routing == "off"
    out = UserConfig(
        learned_routing="shadow",
        learned_router_artefact="/tmp/x.json").to_dict()
    assert out["learned_routing"] == "shadow"
    assert out["learned_router_artefact"] == "/tmp/x.json"
    assert UserConfig.from_dict(out).learned_router_artefact == "/tmp/x.json"


def test_learned_routing_update_validation_is_strict():
    assert validate_handed_off_updates(
        {"learned_routing": "shadow"}) == {"learned_routing": "shadow"}
    assert validate_handed_off_updates(
        {"learned_routing": "off"}) == {"learned_routing": "off"}
    with pytest.raises(ValueError, match="learned_routing"):
        validate_handed_off_updates({"learned_routing": "active"})
    with pytest.raises(ValueError, match="learned_routing"):
        validate_handed_off_updates({"learned_routing": True})
    assert validate_handed_off_updates(
        {"learned_router_artefact": "/tmp/a.json"}) == {
            "learned_router_artefact": "/tmp/a.json"}


# ── Dataset builder ───────────────────────────────────────────────


def _task_records():
    task_one = {
        "delegation_log": [
            {"agent": "supervisor", "timestamp": 1000.0,
             "decision": {"next": "researcher",
                          "reason": "Find the release notes",
                          "briefing": "Check the changelog page"}},
            # K-02's offered/called record: no decision — skipped.
            {"agent": "researcher", "timestamp": 1005.0,
             "tools_offered": ["search_web"], "tools_called": ["search_web"]},
            {"agent": "supervisor", "timestamp": 1010.0,
             "decision": {"next": "coder", "reason": "Fix it",
                          "briefing": "", "denied": True}},
            {"agent": "supervisor", "timestamp": 1020.0,
             "decision": {"next": "END", "reason": "done", "briefing": ""}},
        ],
        "model_routing": [
            {"agent": "researcher", "provider": "google_gemini",
             "model": "gemini-3.5-flash", "tier": "small",
             "routing_source": "auto", "escalated_from": None,
             "timestamp": 1001.0},
        ],
        "has_images": False,
    }
    task_two = {
        "delegation_log": [
            {"agent": "supervisor", "timestamp": 2000.0,
             "decision": {"next": "coder",
                          "reason": "Refactor the api endpoint",
                          "briefing": "step 1 do x\nstep 2 do y\nstep 3 do z"}},
        ],
        "model_routing": [
            {"agent": "coder", "provider": "google_gemini",
             "model": "gemini-3.5-flash", "tier": "small",
             "routing_source": "auto", "escalated_from": None,
             "timestamp": 2001.0},
            {"agent": "coder", "provider": "google_gemini",
             "model": "gemini-2.5-pro", "tier": "strong",
             "routing_source": "escalation",
             "escalated_from": "google_gemini:gemini-3.5-flash",
             "timestamp": 2002.0},
        ],
    }
    outcomes = [
        {"provider_name": "google_gemini", "model_name": "gemini-3.5-flash",
         "agent_name": "researcher", "outcome": "success", "timestamp": 1006},
    ]
    return [task_one, task_two], outcomes


def test_dataset_builder_joins_serving_model_and_outcome():
    records, outcomes = _task_records()
    export = build_dataset(records, outcomes)
    assert len(export.rows) == 2
    first, second = export.rows

    assert first.agent == "researcher"
    assert first.provider == "google_gemini"
    assert first.model == "gemini-3.5-flash"
    assert first.served_tier == 0
    assert first.success is True
    assert first.escalated is False
    assert first.has_images is False
    assert first.explicit_tier is None
    assert first.length_band == "short"
    assert first.wants_code is False

    # The coder delegation escalated, and no outcome row joins it:
    # it stays unlabelled rather than inheriting a guessed result.
    assert second.agent == "coder"
    assert second.model == "gemini-3.5-flash"  # the FIRST (auto) record serves
    assert second.served_tier == 0
    assert second.escalated is True
    assert second.success is None
    assert second.wants_code is True
    # The signals module's loose step count is line-anchored: the
    # two line-start "step N" markers count, the mid-line one in
    # the reason does not — the builder reports the measurement
    # as the signals module defines it.
    assert second.step_count == 2

    report = export.report()
    assert report["delegations"] == 2
    assert report["denied_excluded"] == 1
    assert report["labelled"] == 1
    assert report["unlabelled"] == 1
    assert report["tier_spread"] == {"small": 1}
    assert report["label_mix"] == {"verifier": 0, "bare_outcome": 1}
    assert report["coverage"] == {"google_gemini:gemini-3.5-flash": 1}


def test_dataset_builder_reads_verifier_verdicts_when_present():
    records, outcomes = _task_records()
    records[0]["verifier_verdicts"] = {1000.0: "fail"}
    export = build_dataset(records, outcomes)
    first = export.rows[0]
    # The bare outcome stays as recorded; the verdict is its own
    # field and outranks it as the training label.
    assert first.success is True
    assert first.verifier_verdict == "fail"
    assert export.report()["label_mix"] == {"verifier": 1, "bare_outcome": 0}


def test_outcome_store_rows_feed_the_builder(tmp_path):
    store = ModelOutcomeStore(tmp_path / "routing.db")
    store.record("google_gemini", "gemini-3.5-flash", "researcher", "failure")
    store.record("google_gemini", "gemini-3.5-flash", "researcher", "success")
    rows = store.rows()
    assert [r["outcome"] for r in rows] == ["failure", "success"]
    assert rows[0]["agent_name"] == "researcher"


# ── The honesty gate + training pipeline ──────────────────────────


def test_training_honesty_gate_refuses_below_500_labelled_rows():
    rows = [_row(ts=float(i)) for i in range(60)]
    report = train_scorer(rows)  # the production default gate
    assert report.status == "insufficient_data"
    assert report.labelled_rows == 60
    assert report.min_rows == 500
    assert report.artefact is None
    assert report.bakeoff is None
    assert "60" in report.reason and "500" in report.reason


def test_training_pipeline_runs_end_to_end_on_a_fixture(tmp_path):
    # A learnable pattern: short coder tasks succeed on the small
    # tier; reasoning planner tasks succeed on the strong tier.
    rows = []
    for i in range(30):
        rows.append(_row(agent="coder", text="fix the typo", tier=0,
                         success=i >= 3, ts=float(i)))
    for i in range(30):
        rows.append(_row(agent="planner",
                         text="Prove the derivation, step by step",
                         tier=2, success=i >= 6, ts=100.0 + i))
    report = train_scorer(rows, min_rows=50)
    assert report.status == "trained"
    assert report.labelled_rows == 60
    assert report.train_rows + report.holdout_rows == 60
    assert report.label_mix == {"verifier": 0, "bare_outcome": 60}
    assert report.bakeoff["holdout_rows"] == report.holdout_rows
    assert "counterfactual" in report.bakeoff["note"]

    # Deterministic on a fixed snapshot: same input, same report.
    assert train_scorer(rows, min_rows=50).to_dict() == report.to_dict()

    # The artefact round-trips into a working scorer.
    path = save_artefact(report.artefact, tmp_path / "learned.json")
    scorer = load_scorer(path)
    assert scorer is not None
    need = scorer.need_tier(TaskSignals(agent="coder", task_text="fix the typo"), 1)
    assert need is not None
    assert need[0] == 0
    assert "of the last" in need[1]
    # A bucket the fixture never taught: no opinion, never a guess.
    assert scorer.need_tier(
        TaskSignals(agent="researcher", task_text="fix the typo"), 1) is None


def test_pipeline_writes_no_artefact_when_data_is_insufficient(tmp_path):
    records, outcomes = _task_records()
    target = tmp_path / "learned.json"
    target.write_text("previous-good-artefact")
    report = run_training_pipeline(records, outcomes, artefact_path=target)
    assert report.status == "insufficient_data"
    # A refused retrain never replaces what was there.
    assert target.read_text() == "previous-good-artefact"


def test_pipeline_from_task_records_trains_and_saves(tmp_path):
    records = []
    outcomes = []
    for i in range(12):
        ts = 3000.0 + i * 10
        records.append({
            "delegation_log": [
                {"agent": "supervisor", "timestamp": ts,
                 "decision": {"next": "researcher",
                              "reason": "Find the release notes",
                              "briefing": "Check the changelog page"}}],
            "model_routing": [
                {"agent": "researcher", "provider": "google_gemini",
                 "model": "gemini-3.5-flash", "tier": "small",
                 "routing_source": "auto", "escalated_from": None,
                 "timestamp": ts + 1}],
        })
        outcomes.append({
            "provider_name": "google_gemini",
            "model_name": "gemini-3.5-flash", "agent_name": "researcher",
            "outcome": "failure" if i == 0 else "success",
            "timestamp": int(ts + 5)})
    target = tmp_path / "learned.json"
    report = run_training_pipeline(
        records, outcomes, min_rows=10, artefact_path=target)
    assert report.status == "trained"
    assert report.labelled_rows == 12
    assert target.exists()
    assert load_scorer(target) is not None


# ── The scorer's rules ────────────────────────────────────────────


def test_scorer_picks_the_cheapest_tier_that_clears_the_bar():
    text = "summarise the meeting notes"
    scorer = LearnedScorer({_bucket("researcher", text): {
        0: _cell(ok=4, bad=1),    # rate 0.8, 5 samples — clears
        2: _cell(ok=10, bad=0),   # perfect, but not the cheapest
    }})
    need = scorer.need_tier(TaskSignals(agent="researcher", task_text=text), 1)
    assert need is not None
    assert need[0] == 0
    assert need[1] == (
        "tasks like this succeeded on small tier 4 of the last 5 times")


def test_scorer_falls_back_to_the_best_record_when_none_clears():
    text = "summarise the meeting notes"
    scorer = LearnedScorer({_bucket("researcher", text): {
        0: _cell(ok=1, bad=4),   # rate 0.2
        2: _cell(ok=5, bad=5),   # rate 0.5 — the best available record
    }})
    need = scorer.need_tier(TaskSignals(agent="researcher", task_text=text), 1)
    assert need is not None
    assert need[0] == 2
    assert "5 of the last 10" in need[1]


def test_scorer_has_no_opinion_on_a_thin_record():
    text = "summarise the meeting notes"
    scorer = LearnedScorer({_bucket("researcher", text): {
        0: _cell(ok=2, bad=0),   # 2 samples < MIN_BUCKET_SAMPLES
    }})
    assert scorer.need_tier(
        TaskSignals(agent="researcher", task_text=text), 1) is None
    assert LearnedScorer({}).need_tier(
        TaskSignals(agent="researcher", task_text=text), 1) is None


def test_scorer_verifier_rows_outweigh_bare_outcomes():
    text = "summarise the meeting notes"
    # Bare record alone: 4 ok / 1 bad = 0.8 clears. With two
    # verifier FAILURES double-weighted, the rate drops below the
    # bar and the strong tier's clean record wins instead.
    scorer = LearnedScorer({_bucket("researcher", text): {
        0: _cell(ok=4, bad=1, v_bad=2),
        2: _cell(ok=5, bad=0),
    }})
    need = scorer.need_tier(TaskSignals(agent="researcher", task_text=text), 1)
    assert need is not None
    assert need[0] == 2


def test_artefact_round_trip_and_format_guard(tmp_path):
    scorer = LearnedScorer(
        {_bucket("coder", "fix the typo"): {0: _cell(ok=9, bad=1)}},
        trained_on_rows=10)
    path = save_artefact(scorer.to_artefact(), tmp_path / "a.json")
    loaded = load_scorer(path)
    assert loaded is not None
    assert loaded.trained_on_rows == 10
    assert loaded.need_tier(
        TaskSignals(agent="coder", task_text="fix the typo"), 1)[0] == 0
    # Wrong format / missing file: no scorer, never an exception.
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": 99}')
    assert load_scorer(bad) is None
    assert load_scorer(tmp_path / "missing.json") is None


# ── The route() seam ──────────────────────────────────────────────


def test_route_need_fn_seam_swaps_only_the_need():
    signals = TaskSignals(
        agent="coder", task_text="update the readme links please")
    default = route(signals, _candidates(), History())
    assert default.model == "gemini-3.5-flash"  # the incumbent stands
    forced = route(
        signals, _candidates(), History(),
        need_fn=lambda sig, tier: (2, "a forced need for the test"))
    assert forced.model == "gemini-2.5-pro"
    assert forced.tier == 2
    assert "A forced need for the test" in forced.reason


# ── Shadow mode through the S14 seam ──────────────────────────────

_REASONING_TEXT = "Prove the derivation, step by step, and check every case"


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    """resolve_for_specialist inputs + a trained fixture artefact
    whose record disagrees with the heuristic on a reasoning task:
    the table says standard tier serves tasks like this 9 of 10
    times, so the shadow choice lands below the strong tier the
    heuristic serves."""
    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry())
    monkeypatch.setattr(engine_state, "configured_providers", ["google_gemini"])
    monkeypatch.setattr(engine_state, "health_manager", FakeHealth())
    monkeypatch.setattr(
        model_routing, "collect_history", lambda candidates, **kw: History())
    bus = CapturingBus()
    monkeypatch.setattr(learned_router, "event_bus", bus)
    factory_providers = {}

    def _factory(provider_name, model, secret_store, **kw):
        return factory_providers[(provider_name, model)]

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", _factory)
    scorer = LearnedScorer(
        {_bucket("planner", _REASONING_TEXT): {1: _cell(ok=9, bad=1)}},
        trained_on_rows=10)
    artefact = save_artefact(scorer.to_artefact(), tmp_path / "learned.json")
    return {"bus": bus, "factory_providers": factory_providers,
            "artefact": str(artefact)}


def _shadow_state():
    return {
        "task_id": "p3a-test-task",
        "session_id": "p3a-test-session",
        "messages": [{"role": "user", "content": "plan this"}],
        "original_prompt": "plan this",
        "delegation_log": [
            {"agent": "supervisor", "timestamp": 1.0,
             "decision": {"next": "planner", "reason": _REASONING_TEXT,
                          "briefing": ""}}],
    }


def _resolve(user_config, run_provider):
    return resolve_for_specialist(
        agent="planner", state=_shadow_state(),
        config={"configurable": {
            "user_config": user_config, "secret_store": object()}},
        run_provider=run_provider)


def test_shadow_logs_its_choice_and_never_alters_the_served_one(shadow_env):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro")
    shadow_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash")
    cfg = UserConfig(
        model_routing="auto", learned_routing="shadow",
        learned_router_artefact=shadow_env["artefact"])
    provider, context = _resolve(cfg, run)
    # The heuristic's choice serves: the strong model, constructed
    # by the factory — exactly the S14 outcome.
    assert provider is strong
    assert context.choice.model == "gemini-2.5-pro"
    # And the shadow record carries BOTH choices, marked as shadow.
    shadow_events = [
        e for e in shadow_env["bus"].events
        if (e.payload.get("output_payload") or {}).get("source")
        == "learned-shadow"]
    assert len(shadow_events) == 1
    out = shadow_events[0].payload["output_payload"]
    assert out["heuristic_model"] == "gemini-2.5-pro"
    assert out["heuristic_tier"] == "strong"
    assert out["model"] == "gemini-3.5-flash"
    assert out["agree"] is False
    assert "9 of the last 10" in out["reason"]


def test_shadow_off_by_default_publishes_nothing(shadow_env):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro")
    shadow_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash")
    provider, _context = _resolve(UserConfig(model_routing="auto"), run)
    assert provider is strong
    assert shadow_env["bus"].events == []


def test_shadow_without_an_artefact_is_inert(shadow_env, tmp_path):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro")
    shadow_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash")
    cfg = UserConfig(
        model_routing="auto", learned_routing="shadow",
        learned_router_artefact=str(tmp_path / "no-such-file.json"))
    provider, _context = _resolve(cfg, run)
    assert provider is strong
    assert shadow_env["bus"].events == []


def test_shadow_failure_is_invisible_to_the_run(shadow_env, monkeypatch):
    strong = FakeProvider("google_gemini", "gemini-2.5-pro")
    shadow_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash")

    def _boom(user_config):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(learned_router, "get_scorer", _boom)
    cfg = UserConfig(
        model_routing="auto", learned_routing="shadow",
        learned_router_artefact=shadow_env["artefact"])
    provider, context = _resolve(cfg, run)
    assert provider is strong
    assert context.choice.model == "gemini-2.5-pro"
    assert shadow_env["bus"].events == []


def test_shadow_no_opinion_is_published_as_such(shadow_env):
    # The artefact's tables cover a different bucket: the scorer
    # has no record for this delegation, follows the heuristic,
    # and the published basis says so plainly.
    strong = FakeProvider("google_gemini", "gemini-2.5-pro")
    shadow_env["factory_providers"][("google_gemini", "gemini-2.5-pro")] = strong
    run = FakeProvider("google_gemini", "gemini-3.5-flash")
    cfg = UserConfig(
        model_routing="auto", learned_routing="shadow",
        learned_router_artefact=shadow_env["artefact"])
    state = _shadow_state()
    state["delegation_log"] = [
        {"agent": "supervisor", "timestamp": 1.0,
         "decision": {"next": "planner",
                      "reason": "Prove the derivation for the api "
                                "endpoint, step by step",
                      "briefing": ""}}]
    provider, _context = resolve_for_specialist(
        agent="planner", state=state,
        config={"configurable": {
            "user_config": cfg, "secret_store": object()}},
        run_provider=run)
    assert provider is strong
    shadow_events = [
        e for e in shadow_env["bus"].events
        if (e.payload.get("output_payload") or {}).get("source")
        == "learned-shadow"]
    assert len(shadow_events) == 1
    out = shadow_events[0].payload["output_payload"]
    assert "no empirical record" in out["reason"]
    assert out["agree"] is True  # it followed the heuristic, openly
