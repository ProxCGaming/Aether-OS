"""Wave 2 (Builder I) — cost remainder.

- N-25 [S3] Ledger silently records a half-known call as fully
  "priced" at half tokens — ``cost/ledger.py:414,426-441``: estimation
  runs only when BOTH token counts are None; exactly-one-None coerces
  the other to 0 and labels the row ``priced``.
- N-27 [S3] Usage ledger query loads the whole (never-pruned)
  history — ``cost/ledger.py:250-255``: ``fetchall()`` + totals
  aggregated in Python, no SQL aggregation.
- N-28 [S3] Context breakdown rewrites *any* model containing "2.5"
  to a different model — ``cost/context_breakdown.py:501-503``.
"""

import pytest

from aether_engine.cost import ledger as ledger_module
from aether_engine.cost import pricing
from aether_engine.cost.context_breakdown import get_context_breakdown
from aether_engine.cost.ledger import UsageLedger, record_provider_call
from aether_engine.memory import session_store


@pytest.fixture
def global_ledger(tmp_path, monkeypatch):
    led = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", led)
    return led


# ---------------------------------------------------------------------------
# N-25 — a half-known call is estimated, never silently "priced"
# ---------------------------------------------------------------------------


def test_n25_half_known_call_estimates_the_missing_side(
        global_ledger, monkeypatch):
    monkeypatch.setattr(
        pricing, "estimate_token_counts", lambda **kw: (1000, 40))
    row = record_provider_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=1000, output_tokens=None,
        messages=[{"role": "user", "content": "hi"}], output_text="",
    )
    assert row is not None
    # The reported side stays exact; only the missing side is filled
    # from the estimate, and the row is labelled estimated.
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 40
    assert row["pricing_status"] == "estimated"


def test_n25_half_known_call_without_estimate_is_not_labelled_priced(
        global_ledger, monkeypatch):
    """When the missing side cannot even be estimated, the row must
    still not claim to be exactly priced."""
    monkeypatch.setattr(pricing, "estimate_token_counts", lambda **kw: None)
    row = record_provider_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=None, output_tokens=500,
        messages=None, output_text="answer",
    )
    assert row is not None
    assert row["output_tokens"] == 500
    assert row["pricing_status"] == "estimated"


def test_n25_fully_reported_call_stays_priced(global_ledger, monkeypatch):
    """Guard: both counts reported → exact pricing, unchanged."""
    monkeypatch.setattr(
        pricing, "estimate_token_counts",
        lambda **kw: pytest.fail("estimation must not run"))
    row = record_provider_call(
        model_name="gemini-3.5-flash", provider_name="google_gemini",
        input_tokens=1000, output_tokens=500,
    )
    assert row is not None
    assert row["pricing_status"] == "priced"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500


# ---------------------------------------------------------------------------
# N-27 — totals are aggregated in SQL over the same filter
# ---------------------------------------------------------------------------


def test_n27_query_aggregates_totals_in_sql(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.db")
    for i in range(5):
        ledger.record_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=100 + i, output_tokens=50, cost_usd=0.01 * (i + 1),
            session_id="s1", agent_name="coder", timestamp=1_700_000_000 + i)
    ledger.record_call(
        model_name="gpt-4o", provider_name="openai",
        input_tokens=7, output_tokens=3, cost_usd=0.5,
        session_id="s2", agent_name=None, timestamp=1_700_000_100)

    statements: list[str] = []
    original = UsageLedger._get_conn

    def traced(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(UsageLedger, "_get_conn", traced)
    result = ledger.query()

    aggregate = [
        s for s in statements
        if "SUM(" in s.upper() and "GROUP BY" in s.upper()
    ]
    assert aggregate, (
        "query() must aggregate by-model/by-agent totals in SQL, "
        f"not by scanning every row in Python; statements: {statements}")
    # The totals still reconcile exactly with the returned entries.
    entries = result["entries"]
    totals = result["totals"]
    assert len(entries) == 6
    assert totals["total_cost_usd"] == pytest.approx(
        sum(e["cost_usd"] for e in entries))
    assert totals["input_tokens"] == sum(e["input_tokens"] for e in entries)
    assert totals["output_tokens"] == sum(
        e["output_tokens"] for e in entries)
    assert totals["by_model"]["gpt-4o"]["calls"] == 1
    assert totals["by_agent"]["unknown"]["calls"] == 1
    assert totals["by_agent"]["coder"]["calls"] == 5


# ---------------------------------------------------------------------------
# N-28 — the breakdown reports the model that actually ran
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", test_db)
    session_store.init_db()
    return test_db


def test_n28_model_containing_2_5_is_not_rewritten(isolated_db):
    session_id = session_store.create_session("N-28")
    session_store.add_message(session_id, "user", "hello")
    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-2.5-pro", is_graph_task=False)
    assert breakdown["model"] == "gemini-2.5-pro"


def test_n28_qwen_2_5_is_not_rewritten(isolated_db):
    session_id = session_store.create_session("N-28 qwen")
    session_store.add_message(session_id, "user", "hello")
    breakdown = get_context_breakdown(
        session_id=session_id, model="qwen2.5-7b", is_graph_task=False)
    assert breakdown["model"] == "qwen2.5-7b"
