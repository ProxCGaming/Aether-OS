"""S1-N2: knowledge-graph search must treat the query as literal text.

search_facts built its LIKE pattern from the raw query, so a query
containing % or _ silently became a wildcard search ("100%" matched
every fact). The metacharacters are escaped now (ESCAPE '\\'), and
these tests pin the literal semantics.
"""
from unittest.mock import patch

import pytest

import aether_engine.memory.knowledge_graph as kg_mod


@pytest.fixture
def isolated_kg(tmp_path):
    """Patch the KG DB path + audit logger onto tmp files (the
    test_phase10_3_kg_dedup recipe — never the real ~/.aether DB)."""
    test_db = tmp_path / "kg_like.db"
    with (
        patch("aether_engine.memory.knowledge_graph.DB_PATH", test_db),
        patch("aether_engine.memory.knowledge_graph._audit_logger.log_event"),
    ):
        kg_mod.init_kg_db()
        yield test_db


def _seed(facts):
    with kg_mod._get_conn() as conn:
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, first_seen, last_updated)"
            " VALUES ('e1', 'User', 'Person', 1, 1)"
        )
        for i, text in enumerate(facts):
            conn.execute(
                "INSERT INTO facts (id, entity_id, fact_text, timestamp, confidence, status)"
                " VALUES (?, 'e1', ?, ?, 0.9, 'active')",
                (f"f{i}", text, 100 + i),
            )
        conn.commit()


def _texts(rows):
    return sorted(r["fact_text"] for r in rows)


def test_percent_in_query_matches_literally(isolated_kg):
    _seed(["User scored 100% on the test", "User scored 100 points"])
    assert _texts(kg_mod.search_facts("100%")) == ["User scored 100% on the test"]


def test_bare_percent_does_not_match_everything(isolated_kg):
    _seed(["User scored 100% on the test", "User lives in Kolkata"])
    assert _texts(kg_mod.search_facts("%")) == ["User scored 100% on the test"]


def test_underscore_in_query_matches_literally(isolated_kg):
    _seed(["User handle is a_b", "User handle is axb"])
    assert _texts(kg_mod.search_facts("a_b")) == ["User handle is a_b"]


def test_plain_substring_search_is_unchanged(isolated_kg):
    _seed(["User scored 100% on the test", "User scored 100 points", "User lives in Kolkata"])
    assert _texts(kg_mod.search_facts("100")) == [
        "User scored 100 points",
        "User scored 100% on the test",
    ]
    assert _texts(kg_mod.search_facts("Kolkata")) == ["User lives in Kolkata"]
