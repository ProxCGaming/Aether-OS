# ADR 0017: Three-Tier Memory System (FTS5 Episodic Memory & Semantic Knowledge Graph)

## Date
2026-09-20

## Context

Prior to Phase 6, AETHER OS operated statelessly across tasks. Each new user task started with zero awareness of previous interactions, decisions, or user preferences. In-flight tasks also had no structured fact extraction.

Phase 6 introduces a multi-tier memory architecture to give AETHER cross-session continuity:
1. **Tier 1: Working Memory Compaction (Single-task, in-process)**: Prunes and compacts message history within long-running tasks. (Deferred by user instruction during Phase 6 alignment to a subsequent phase).
2. **Tier 2: Episodic Memory (Cross-session)**: Retains concise summaries of completed tasks so future related tasks can retrieve relevant past context.
3. **Tier 3: Semantic Knowledge Graph (Structured Facts, SQLite)**: Extracts durable entities and facts mentioned across sessions into SQLite storage.

### Hard Architectural Rule
Every LLM call introduced in this phase—extraction for the knowledge graph and summarization—MUST route exclusively through LiteLLM. Direct SDK calls (e.g. OpenAI or Google GenAI directly) are strictly prohibited.

## Decision

1. **Deferral of Tier 1 (Working Memory Compaction)**:
   Working memory compaction is intentionally deferred to keep initial scope focused on cross-session persistence and structured fact extraction. Checkpoint database storage remains managed by LangGraph's persistent checkpointer (`~/.aether/checkpoints.db`).

2. **Tier 2: Episodic Memory via SQLite FTS5 (BM25 Ranking)**:
   - Instead of running a heavyweight vector database (such as ChromaDB with deep neural network embedding inference) which imposes substantial RAM and battery/CPU overhead, AETHER adopts **native SQLite FTS5 full-text search with BM25 probabilistic relevance ranking** and Porter stemming (`tokenize = 'porter unicode61'`).
   - **Rationale**:
     - **0 MB extra RAM**: Uses Python's built-in `sqlite3` without background daemon processes or model weights loaded into memory.
     - **0% CPU / Battery draw**: BM25 searches execute in $< 1\text{ms}$ using disk B-tree inverted indexes.
     - **0 external dependencies**: Eliminates multi-megabyte external pip packages (ChromaDB, ONNX, torch, numpy).
     - **Stemming & Tag Relevance**: Porter stemming handles grammatical variations (e.g., "debug", "debugging", "debugged"), and keyword tags captured at task completion bridge semantic synonyms.
   - Stored in SQLite at `~/.aether/memory.db`.
   - At the conclusion of each task (succeeded, failed, cancelled), a task record is stored with prompt, summary, tags, and outcome.
   - At the beginning of a new task, the Supervisor queries episodic memory using BM25 ranking (`LIMIT 3`). If relevant past episodes exist, they are injected into the Supervisor's prompt for routing.
   - Operations are audited via `EPISODIC_MEMORY_STORED` and `EPISODIC_MEMORY_RETRIEVED`.

3. **Tier 3: Semantic Knowledge Graph**:
   - Unified in SQLite at `~/.aether/memory.db` to enable relational integrity between facts and episodic tasks.
   - Two core tables:
     - `entities (id TEXT PRIMARY KEY, name TEXT UNIQUE, entity_type TEXT, first_seen INTEGER, last_updated INTEGER)`
     - `facts (id TEXT PRIMARY KEY, entity_id TEXT REFERENCES entities(id), fact_text TEXT, source_task_id TEXT, timestamp INTEGER, confidence REAL)`
   - At task conclusion, execution runs synchronously. A LiteLLM extraction call extracts entities and facts from the conversation. Trivial conversational greetings (e.g. single-turn greetings) are heuristically skipped to prevent wasted API calls.
   - Fact conflict reconciliation and proactive suggestions are explicitly deferred to Phases 8 and 9; facts are appended with timestamps and source references.
   - Writes are audited via `KNOWLEDGE_FACT_STORED`.

## Consequences

- **Positive**:
  - AETHER remembers prior user projects, preferences, and relevant task history across sessions with near-zero resource consumption.
  - Zero dependencies beyond standard Python library.
  - Sub-millisecond lookup latency.
  - Complete auditability through dedicated audit log entries.
- **Negative / Trade-offs**:
  - Pure FTS5 relies on lexical matching, word stems, and tags rather than latent geometric embedding dimensions, though this is heavily mitigated by auto-generated tags and entity links.
  - Synchronous completion adds a brief 1-2s latency on meaningful task completions to guarantee persistence before marking complete.
  - Deferring Tier 1 compaction means single-task message histories still accumulate in checkpoints until later addressed.
