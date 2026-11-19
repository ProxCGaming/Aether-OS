"""S22 / SPEC-6B C1 (FF5-03): the Profile read contract.

Pins what the spec note promises for Batch 5 board 1:

* the facade's ``user_profile()`` keeps returning exactly the
  S6 view's sections — the defaults reproduce today's lists, so
  existing callers (the fast-chat digest) see no change;
* pagination is per section (Rule 2): ``limit`` + ``offset``
  slice every section, the people section pages over its items
  in grouped order, and ``totals`` always reports the unpaged
  per-section counts;
* every item carries ``source_label``, derived at the facade
  from provenance + timestamp: "Set by you" for stated facts,
  "From chat · <day> <Mon>" for inferred ones (plain "From chat"
  when the record has no usable timestamp);
* the wire sender (MEMORY_PROFILE_REQUEST) answers the paginated
  profile, and an unavailable backend answers profile=None +
  error — the explicit unavailable state, never an empty success
  (Rule 1).

All stores are tmp files (the S6/S8 fixture patterns); no live
provider calls.
"""
import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from aether_engine.memory import knowledge_graph as kg
from aether_engine.memory.facade import NativeMemoryBackend
from aether_engine.routes import ws_connection
from aether_engine.routes.ws_memory_personal import send_memory_profile_result

NOW = int(time.time())


@pytest.fixture()
def kg_db(tmp_path):
    db = tmp_path / "kg.db"
    with patch.object(kg, "DB_PATH", db), \
            patch.object(kg, "_audit_logger") as audit:
        audit.log_event = lambda *a, **k: None
        kg.init_kg_db()
        yield db


def _insert_fact(db_path, fid, entity_id, entity_name, entity_type, text, *,
                 ts=NOW, conf=0.9, provenance="stated", importance=0.8,
                 status="active", superseded_by=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO entities (id, name, entity_type, first_seen, last_updated) "
        "VALUES (?, ?, ?, ?, ?)", (entity_id, entity_name, entity_type, ts, ts))
    conn.execute(
        "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, "
        "confidence, status, superseded_by, provenance, importance) "
        "VALUES (?, ?, ?, 'seed', ?, ?, ?, ?, ?, ?)",
        (fid, entity_id, text, ts, conf, status, superseded_by,
         provenance, importance))
    conn.commit()
    conn.close()


def _seed_profile(kg_db):
    """The S6 seed: 2 user facts, 1 person, 1 preference, plus a
    superseded fact and a non-profile project fact that must
    never appear."""
    _insert_fact(kg_db, "f-name", "e-user", "User", "Person",
                 "User is called Arindam", conf=0.95,
                 provenance="stated", importance=0.9)
    _insert_fact(kg_db, "f-tendency", "e-user", "User", "Person",
                 "User tends to work late at night", conf=0.6,
                 provenance="inferred", importance=0.4)
    _insert_fact(kg_db, "f-old", "e-user", "User", "Person",
                 "User lives in Delhi", conf=0.8,
                 provenance="stated", importance=0.8,
                 status="superseded", superseded_by="f-name")
    _insert_fact(kg_db, "f-sister", "e-priya", "Priya", "Person",
                 "Priya is the user's sister", conf=0.9,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-style", "e-style", "Response style", "Preference",
                 "User prefers short, direct answers", conf=0.9,
                 provenance="stated", importance=0.8)
    _insert_fact(kg_db, "f-proj", "e-proj", "Aether-OS", "Project",
                 "Aether-OS uses SQLite", conf=0.9,
                 provenance="stated", importance=0.8)


# ----------------------------------------------------------------------
# Backward compatibility: the defaults reproduce today's view
# ----------------------------------------------------------------------

def test_default_profile_reproduces_todays_sections(kg_db):
    from aether_engine.memory import profile as profile_mod

    _seed_profile(kg_db)
    backend = NativeMemoryBackend()
    view = backend.user_profile()

    assert [i["fact_id"] for i in view["user"]] == ["f-name", "f-tendency"]
    assert list(view["people"]) == ["Priya"]
    assert [i["fact_id"] for i in view["people"]["Priya"]] == ["f-sister"]
    assert [i["fact_id"] for i in view["preferences"]] == ["f-style"]
    # The facade default IS the view's own derivation, totals aside.
    direct = profile_mod.build_user_profile(backend)
    assert view["user"] == direct["user"]
    assert view["people"] == direct["people"]
    assert view["preferences"] == direct["preferences"]
    assert view["totals"] == {"user": 2, "people": 1, "preferences": 1}


# ----------------------------------------------------------------------
# source_label (C1 addition b)
# ----------------------------------------------------------------------

def test_source_label_strings(kg_db):
    _seed_profile(kg_db)
    view = NativeMemoryBackend().user_profile()
    by_id = {i["fact_id"]: i for i in view["user"]}
    assert by_id["f-name"]["source_label"] == "Set by you"
    # Inferred with a real timestamp: "From chat · <day> <Mon>"
    # of the fact's own date.
    expected_day = datetime.fromtimestamp(NOW)
    assert by_id["f-tendency"]["source_label"] == (
        f"From chat · {expected_day.day} {expected_day.strftime('%b')}")


def test_source_label_for_a_known_date(kg_db):
    # Midday local time, so the rendered day is 17 Nov in any
    # timezone the suite runs in.
    ts = int(datetime(2026, 11, 17, 12, 0).timestamp())
    _insert_fact(kg_db, "f-chat", "e-user", "User", "Person",
                 "User mentioned a Kolkata trip", ts=ts,
                 provenance="inferred", importance=0.5)
    view = NativeMemoryBackend().user_profile()
    item = next(i for i in view["user"] if i["fact_id"] == "f-chat")
    assert item["source_label"] == "From chat · 17 Nov"


def test_source_label_without_a_timestamp(kg_db):
    _insert_fact(kg_db, "f-notime", "e-user", "User", "Person",
                 "User likes monsoon rain", ts=0,
                 provenance="inferred", importance=0.5)
    view = NativeMemoryBackend().user_profile()
    item = next(i for i in view["user"] if i["fact_id"] == "f-notime")
    assert item["source_label"] == "From chat"


# ----------------------------------------------------------------------
# Per-section pagination + totals (C1 addition a, Rule 2)
# ----------------------------------------------------------------------

def _seed_many(kg_db):
    # Five user facts, distinct importance fixing the trust order.
    for n in range(5):
        _insert_fact(kg_db, f"f-u{n}", "e-user", "User", "Person",
                     f"User fact number {n}", importance=0.9 - n * 0.1)
    # Two people: Asha with 2 facts (leads), Bikram with 1.
    _insert_fact(kg_db, "f-a1", "e-asha", "Asha", "Person",
                 "Asha is a colleague", importance=0.85)
    _insert_fact(kg_db, "f-a2", "e-asha", "Asha", "Person",
                 "Asha reviews the designs", importance=0.75)
    _insert_fact(kg_db, "f-b1", "e-bikram", "Bikram", "Person",
                 "Bikram is a neighbour", importance=0.65)
    # Three preferences.
    for n in range(3):
        _insert_fact(kg_db, f"f-p{n}", "e-pref", "Response style",
                     "Preference", f"Preference number {n}",
                     importance=0.9 - n * 0.1)


def test_pagination_slices_every_section_and_reports_totals(kg_db):
    _seed_many(kg_db)
    backend = NativeMemoryBackend()

    page = backend.user_profile(limit=2, offset=0)
    assert [i["fact_id"] for i in page["user"]] == ["f-u0", "f-u1"]
    assert page["totals"] == {"user": 5, "people": 3, "preferences": 3}
    # The people section pages over its items in grouped order:
    # page 1 is Asha's two facts, Bikram not present yet.
    assert list(page["people"]) == ["Asha"]
    assert [i["fact_id"] for i in page["people"]["Asha"]] == ["f-a1", "f-a2"]
    assert [i["fact_id"] for i in page["preferences"]] == ["f-p0", "f-p1"]

    page2 = backend.user_profile(limit=2, offset=2)
    assert [i["fact_id"] for i in page2["user"]] == ["f-u2", "f-u3"]
    assert list(page2["people"]) == ["Bikram"]
    assert [i["fact_id"] for i in page2["people"]["Bikram"]] == ["f-b1"]
    assert [i["fact_id"] for i in page2["preferences"]] == ["f-p2"]

    tail = backend.user_profile(limit=2, offset=4)
    assert [i["fact_id"] for i in tail["user"]] == ["f-u4"]
    assert tail["people"] == {}
    assert tail["preferences"] == []
    assert tail["totals"] == {"user": 5, "people": 3, "preferences": 3}

    beyond = backend.user_profile(limit=2, offset=50)
    assert beyond["user"] == [] and beyond["people"] == {}
    assert beyond["totals"]["user"] == 5


# ----------------------------------------------------------------------
# The wire sender
# ----------------------------------------------------------------------

class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


def _conn():
    return ws_connection.WsConnection(_FakeSocket(), object(), object())


async def test_profile_sender_answers_the_paginated_profile(kg_db, monkeypatch):
    _seed_many(kg_db)
    backend = NativeMemoryBackend()
    monkeypatch.setattr(
        "aether_engine.routes.websocket.backend_or_none",
        lambda config, context="": backend)
    conn = _conn()
    await send_memory_profile_result(conn, "req-1", 2, 0)
    assert len(conn.ws.sent) == 1
    reply = conn.ws.sent[0]
    assert reply["type"] == "MEMORY_PROFILE_RESPONSE"
    assert reply["request_id"] == "req-1"
    payload = reply["payload"]
    assert payload["backend"] == "native"
    assert payload["profile"]["totals"] == {
        "user": 5, "people": 3, "preferences": 3}
    assert [i["fact_id"] for i in payload["profile"]["user"]] == ["f-u0", "f-u1"]
    assert payload["profile"]["user"][0]["source_label"] == "Set by you"


async def test_profile_sender_reports_an_unavailable_backend(monkeypatch):
    monkeypatch.setattr(
        "aether_engine.routes.websocket.backend_or_none",
        lambda config, context="": None)
    conn = _conn()
    await send_memory_profile_result(conn, "req-2", None, 0)
    payload = conn.ws.sent[0]["payload"]
    # Rule 1: an explicit unavailable state, never an empty profile.
    assert payload["profile"] is None
    assert "unavailable" in payload["error"]
