import sqlite3
import time
import uuid
import json
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from aether_engine.providers.base import BaseProvider
from aether_engine.audit import AuditLogger

_audit_logger = AuditLogger()

logger = logging.getLogger("aether_engine.memory.knowledge_graph")

# Database path is ~/.aether/memory.db
DB_PATH = Path.home() / ".aether" / "memory.db"

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
                    (fact_id, entity_id, item.get("fact", ""), task_id, now, item.get("confidence", 1.0))
                )
                
                _audit_logger.log_event("KNOWLEDGE_FACT_STORED", {
                    "entity_name": entity_name,
                    "fact": item.get("fact", ""),
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
            ORDER BY f.timestamp DESC
        """, (entity_name,))
        return [dict(row) for row in cur.fetchall()]
        
def get_all_facts(limit: int = 100) -> List[Dict[str, Any]]:
    init_kg_db()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.id as fact_id, e.id as entity_id, e.name as entity_name, e.entity_type, f.fact_text, f.timestamp 
            FROM facts f 
            JOIN entities e ON f.entity_id = e.id 
            ORDER BY f.timestamp DESC LIMIT ?
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
            ORDER BY f.timestamp DESC LIMIT ?
        """, (search_term, search_term, limit))
        return [dict(row) for row in cur.fetchall()]
