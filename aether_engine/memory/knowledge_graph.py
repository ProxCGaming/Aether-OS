import sqlite3
import time
import uuid
import json
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from aether_engine.providers.base import BaseProvider
from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig, load_config

_audit_logger = AuditLogger()

logger = logging.getLogger("aether_engine.memory.knowledge_graph")

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

# Phase 10.3 A3: DB file paths the one-time KG dedupe migration
# (dedupe_facts) has already run for in this process. A fresh tmp DB
# in tests gets its own key, so the migration still runs there on
# first init.
_DEDUPED_DB_PATHS = set()

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_kg_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                entity_type TEXT,
                first_seen INTEGER NOT NULL,
                last_updated INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                source_task_id TEXT,
                timestamp INTEGER NOT NULL,
                confidence REAL,
                FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
    # Phase 10.3 A3: collapse any duplicate facts left by the old
    # unconditional INSERT in extract_and_store_facts (once per
    # process per DB file).
    _ensure_kg_deduped()

# ---------------------------------------------------------------------------
# Phase 10.3 A3: KG fact dedupe + confidence-weighted ranking. Before
# this, extract_and_store_facts() did an unconditional INSERT (no dedupe
# on (entity, content) of any kind, no UNIQUE constraint on facts), and
# every retrieval ranked by timestamp DESC only, so a low-confidence
# LLM guess ranked equal to a high-confidence fact of the same age.
#
# Dedupe key: (entity name, fact text), compared case-insensitively
# after the graph's own normalisation — which is none: entity lookup is
# an exact-match on name and fact text is stored verbatim, so the key
# here is strip() + casefold() on both halves. The entity itself is
# NOT merged or renamed by dedupe (case-sensitive entity identity is
# critical/02 finding #11, a separate decision); only the duplicate
# fact rows are collapsed.
#
# Ranking: ORDER BY confidence DESC, timestamp DESC — higher-confidence
# facts rank above lower-confidence ones, all else equal; timestamp
# still breaks ties. Confidence NULL (LLM returned an explicit null,
# or a junk non-numeric value) counts as 0.0, so unknown confidence
# ranks below any stated confidence instead of first (SQLite sorts
# NULL, and TEXT, first under DESC).
# ---------------------------------------------------------------------------

def _normalise_kg_text(text: Optional[str]) -> str:
    """Dedupe-key normalisation for KG entity names / fact text."""
    return (text or "").strip().casefold()


def _confidence_value(row: sqlite3.Row) -> float:
    """Numeric confidence of a fact row; 0.0 for NULL / junk values."""
    try:
        return float(row["confidence"])
    except (TypeError, ValueError):
        return 0.0


def _find_duplicate_fact_id(
    conn: sqlite3.Connection, entity_name: str, fact_text: str
) -> Optional[str]:
    """Id of an existing fact with the same dedupe key, else None.

    Full scan of the facts table with the comparison in Python:
    SQLite's lower() / NOCASE are ASCII-only, while casefold() is
    Unicode-aware, and the table is bounded by A1 pruning (default
    max 5000 facts), so the scan stays cheap and correct.
    """
    target_entity = _normalise_kg_text(entity_name)
    target_fact = _normalise_kg_text(fact_text)
    rows = conn.execute(
        "SELECT f.id, e.name, f.fact_text FROM facts f "
        "JOIN entities e ON f.entity_id = e.id"
    ).fetchall()
    for fact_id, name, text in rows:
        if (_normalise_kg_text(name) == target_entity
                and _normalise_kg_text(text) == target_fact):
            return fact_id
    return None


def dedupe_facts() -> Dict[str, Any]:
    """One-time migration: collapse duplicate KG facts.

    For each (normalised entity name, normalised fact text) group with
    more than one row, the highest-confidence copy survives (ties break
    by earliest timestamp, then lowest id) and the rest are deleted.
    Nothing is silently dropped: the removed rows are logged to the
    audit log (KNOWLEDGE_GRAPH_DEDUPED) with their entity / text.
    Entities left with no facts are removed, mirroring delete_fact().
    Idempotent — a second run finds no duplicates and logs nothing.
    """
    init_kg_db()
    removed: List[Dict[str, Any]] = []
    groups_deduped = 0
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT f.id, e.name, f.fact_text, f.confidence, f.timestamp "
            "FROM facts f JOIN entities e ON f.entity_id = e.id"
        ).fetchall()
        groups: Dict[tuple, List[sqlite3.Row]] = {}
        for row in rows:
            key = (_normalise_kg_text(row["name"]),
                   _normalise_kg_text(row["fact_text"]))
            groups.setdefault(key, []).append(row)
        for members in groups.values():
            if len(members) < 2:
                continue
            groups_deduped += 1
            members.sort(key=lambda r: (-_confidence_value(r),
                                        r["timestamp"], r["id"]))
            for victim in members[1:]:
                conn.execute("DELETE FROM facts WHERE id = ?",
                             (victim["id"],))
                removed.append({
                    "fact_id": victim["id"],
                    "entity_name": victim["name"],
                    "fact_text": victim["fact_text"],
                })
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT DISTINCT entity_id FROM facts)"
        )
        conn.commit()
    result: Dict[str, Any] = {
        "fact_groups_deduped": groups_deduped,
        "facts_removed": len(removed),
    }
    if removed:
        details = dict(result, removed=removed)
        _audit_logger.log_event("KNOWLEDGE_GRAPH_DEDUPED", details)
        logger.warning("KNOWLEDGE_GRAPH_DEDUPED %s", result)
    return result


def _ensure_kg_deduped() -> None:
    """Run the dedupe migration once per process per DB file.

    Called from init_kg_db() so existing deployments heal themselves
    on first use after the upgrade, with no new caller wiring.
    """
    key = str(DB_PATH)
    if key in _DEDUPED_DB_PATHS:
        return
    _DEDUPED_DB_PATHS.add(key)
    dedupe_facts()

async def extract_and_store_facts(
    provider: BaseProvider, 
    user_prompt: str, 
    assistant_response: str, 
    task_id: str
) -> None:
    init_kg_db()
    
    # Skip pure short greetings with no personal information or substance
    pure_greetings = {"hi", "hello", "hey", "hii", "heyy", "sup", "yo"}
    if user_prompt.strip().lower() in pure_greetings and len(assistant_response) < 40:
        return
    if len(user_prompt.strip()) < 3:
        return
        
    system_prompt = """
You are a Knowledge Extraction Engine.
Extract concrete facts from the conversation below about the user or the project.
Return a JSON array of objects, where each object has:
- "entity_name": The subject of the fact (e.g. "User", "Project X", "The API"). Max 3 words.
- "entity_type": e.g. "Person", "Project", "Concept", "Preference".
- "fact": A concise standalone statement (e.g. "User prefers Python over Java", "Project X uses SQLite").
- "confidence": Float between 0.0 and 1.0.

Only extract facts that are actually useful to remember long-term.
If no facts are present, return an empty array [].
Respond ONLY with raw JSON array, no markdown code blocks, no explanations.
"""
    conversation = f"User: {user_prompt}\nAssistant: {assistant_response}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation}
    ]
    
    try:
        response_text = ""
        # The provider returns a stream
        async for chunk in provider.call_stream(messages=messages):
            if chunk.text:
                response_text += chunk.text
                
        response_text = response_text.strip()
        # Clean markdown code blocks if any
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        if response_text.startswith("```"):
            response_text = response_text[3:]
            
        facts_list = json.loads(response_text.strip())
        
        if not isinstance(facts_list, list):
            return
            
        now = int(time.time())
        with _get_conn() as conn:
            for item in facts_list:
                entity_name = item.get("entity_name")
                if not entity_name: continue

                # Phase 10.3 A3: dedupe on (entity, content),
                # case-insensitively (see _normalise_kg_text). Checked
                # before the entity upsert, so a skipped duplicate does
                # not leave behind a fact-less entity row. A duplicate
                # is skipped, not re-inserted; the surviving copy is
                # left untouched (no confidence bump — that is
                # correction of a stored fact, not dedupe, and is out
                # of scope for this phase).
                fact_text = item.get("fact", "")
                if _find_duplicate_fact_id(conn, entity_name, fact_text) is not None:
                    logger.info(
                        "Skipping duplicate KG fact for entity %r: %r",
                        entity_name, fact_text,
                    )
                    _audit_logger.log_event("KNOWLEDGE_FACT_DUPLICATE_SKIPPED", {
                        "entity_name": entity_name,
                        "fact": fact_text,
                        "task_id": task_id,
                    })
                    continue

                # Upsert entity
                cur = conn.cursor()
                cur.execute("SELECT id FROM entities WHERE name = ?", (entity_name,))
                row = cur.fetchone()
                if row:
                    entity_id = row["id"]
                    cur.execute("UPDATE entities SET last_updated = ? WHERE id = ?", (now, entity_id))
                else:
                    entity_id = str(uuid.uuid4())
                    cur.execute(
                        "INSERT INTO entities (id, name, entity_type, first_seen, last_updated) VALUES (?, ?, ?, ?, ?)",
                        (entity_id, entity_name, item.get("entity_type", "Unknown"), now, now)
                    )

                # Insert fact
                fact_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO facts (id, entity_id, fact_text, source_task_id, timestamp, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                    (fact_id, entity_id, fact_text, task_id, now, item.get("confidence", 1.0))
                )

                _audit_logger.log_event("KNOWLEDGE_FACT_STORED", {
                    "entity_name": entity_name,
                    "fact": fact_text,
                    "task_id": task_id
                })
            conn.commit()
    except json.JSONDecodeError:
        logger.warning(f"Fact extraction returned invalid JSON. Raw output: {response_text[:100]}")
    except Exception as e:
        logger.warning(f"Fact extraction failed: {e}")

def get_entity_facts(entity_name: str) -> List[Dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.* 
            FROM facts f 
            JOIN entities e ON f.entity_id = e.id 
            WHERE e.name = ? 
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC
        """, (entity_name,))
        return [dict(row) for row in cur.fetchall()]
        
def get_all_facts(limit: int = 100) -> List[Dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.id as fact_id, e.id as entity_id, e.name as entity_name, e.entity_type, f.fact_text, f.timestamp, f.confidence
            FROM facts f 
            JOIN entities e ON f.entity_id = e.id 
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]

def delete_fact(fact_id: str) -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        cur.execute("DELETE FROM entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM facts)")
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_FACT_DELETED", {"fact_id": fact_id})
    return True

def delete_entity(entity_name: str) -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM entities WHERE name = ?", (entity_name,))
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_ENTITY_DELETED", {"entity_name": entity_name})
    return True

def clear_all_facts() -> bool:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM facts")
        cur.execute("DELETE FROM entities")
        conn.commit()
    _audit_logger.log_event("KNOWLEDGE_GRAPH_CLEARED_ALL", {})
    return True

def search_facts(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        search_term = f"%{query.strip()}%"
        cur.execute("""
            SELECT f.id as fact_id, e.name as entity_name, e.entity_type, f.fact_text, f.timestamp, f.confidence
            FROM facts f
            JOIN entities e ON f.entity_id = e.id
            WHERE e.name LIKE ? OR f.fact_text LIKE ?
            ORDER BY COALESCE(CAST(f.confidence AS REAL), 0.0) DESC, f.timestamp DESC LIMIT ?
        """, (search_term, search_term, limit))
        return [dict(row) for row in cur.fetchall()]

# ---------------------------------------------------------------------------
# Phase 10.3 A1: retention / pruning. Before this, the knowledge graph had
# no pruning, decay, or TTL — entities + facts grew without bound. Bounds
# come from UserConfig (config.py is the single source of the numbers);
# explicit args here override them, and a bound <= 0 disables pruning on
# that axis.
#
# Safety guard — never delete the only record of a fact that is still
# referenced: a fact that is the ONLY fact of its entity is never pruned,
# because deleting it would erase the entity's sole record (nothing else
# points at a fact, and facts only point outward via entity_id /
# source_task_id, so the entity's last fact is the one live reference).
# Kept facts are counted as kept_referenced. Entities left with no facts
# after pruning are removed, mirroring delete_fact().
# ---------------------------------------------------------------------------

def prune_facts(retention_days: Optional[int] = None,
                max_facts: Optional[int] = None,
                *, config: Optional[UserConfig] = None,
                now: Optional[int] = None) -> Dict[str, int]:
    """Prune KG facts by age, then by count (oldest first).

    Age: facts with ``timestamp`` older than ``retention_days`` are
    removed, unless they are their entity's only fact. Count: if more
    than ``max_facts`` remain, the oldest beyond the cap are removed,
    with the same guard. Returns counts and logs them to the audit log
    (KNOWLEDGE_GRAPH_PRUNED).
    """
    cfg = config if config is not None else load_config()
    if retention_days is None:
        retention_days = cfg.kg_fact_retention_days
    if max_facts is None:
        max_facts = cfg.kg_max_facts
    now = int(time.time()) if now is None else int(now)

    init_kg_db()
    pruned = 0
    kept_ids = set()

    with _get_conn() as conn:
        cur = conn.cursor()
        # Snapshot of entities with exactly one fact, taken before any
        # delete: those facts are protected for this whole run.
        sole_entity_ids = {
            r[0] for r in cur.execute(
                "SELECT entity_id FROM facts GROUP BY entity_id HAVING COUNT(*) = 1"
            )
        }

        def _delete_if_unreferenced(fact_id: str, entity_id: str) -> bool:
            nonlocal pruned
            if entity_id in sole_entity_ids:
                kept_ids.add(fact_id)
                return False
            cur.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            pruned += 1
            return True

        if retention_days and retention_days > 0:
            cutoff = now - int(retention_days) * 86400
            rows = cur.execute(
                "SELECT id, entity_id FROM facts WHERE timestamp < ?",
                (cutoff,),
            ).fetchall()
            for fact_id, entity_id in rows:
                _delete_if_unreferenced(fact_id, entity_id)

        if max_facts and max_facts > 0:
            total = cur.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            excess = total - int(max_facts)
            if excess > 0:
                rows = cur.execute(
                    "SELECT id, entity_id FROM facts ORDER BY timestamp ASC"
                ).fetchall()
                for fact_id, entity_id in rows:
                    if excess <= 0:
                        break
                    if _delete_if_unreferenced(fact_id, entity_id):
                        excess -= 1

        cur.execute("DELETE FROM entities WHERE id NOT IN (SELECT DISTINCT entity_id FROM facts)")
        pruned_entities = cur.rowcount
        conn.commit()

    result = {
        "pruned_facts": pruned,
        "kept_referenced": len(kept_ids),
        "pruned_entities": pruned_entities,
        "retention_days": retention_days,
        "max_facts": max_facts,
    }
    _audit_logger.log_event("KNOWLEDGE_GRAPH_PRUNED", result)
    logger.info("KNOWLEDGE_GRAPH_PRUNED %s", result)
    return result
