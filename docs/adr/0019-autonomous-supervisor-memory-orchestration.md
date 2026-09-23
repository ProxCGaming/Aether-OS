# ADR 0019: Autonomous Supervisor Memory Orchestration (Context Preservation & Specialist Isolation)

## Status
Accepted

## Date
2026-09-22

## Context
In AETHER OS, multi-agent workflows route tasks from a Supervisor node to specialists (`planner`, `coder`, `researcher`). Prior to this decision:
1. **Specialist Context Pollution vs. Amnesia**:
   - Dumping raw past chat messages, episodic dumps, and knowledge graph facts directly into the message arrays of worker agents (`coder`, `planner`, `researcher`) quickly exhausted context windows, confused specialist reasoning, and increased token costs.
   - Conversely, omitting context resulted in conversational amnesia where the system could not recall the user's name, past instructions, or prior discussions across messages in the same open chat session.
2. **Disconnected Supervisor Retrieval**:
   - In ADR 0017, episodic memory was queried during Supervisor routing, but the retrieved text was trapped within the Supervisor's routing prompt. When the Supervisor delegated to `planner` or `coder`, that context was lost.
   - Furthermore, `session_id` was not propagated to `run_langgraph_task`, preventing the engine from associating LangGraph execution with the active chat session's history.
   - Knowledge graph extraction heuristically discarded prompts shorter than 15 characters, causing introductions like "My name is Alex" or "I am Sam" to be skipped.

## Decision
We implemented an **Autonomous Memory Orchestration Architecture** where the **Supervisor Agent** acts as an intelligent memory gateway, ensuring specialists remain lightweight, isolated, and focused solely on execution:

1. **Supervisor as the Sole Memory Gateway**:
   Worker agents (`planner`, `coder`, `researcher`) are isolated from raw memory stores. Only the Supervisor agent has access to memory retrieval tools:
   - `get_current_chat_history(limit=6)`: Retrieves recent conversational turns from the currently open chat session (`session_store.get_session(session_id)`), excluding the current pending prompt.
   - `search_knowledge_graph(query)`: Searches persistent entity facts and user preferences from SQLite (`knowledge_graph.search_facts(query)`).
   - `search_past_episodes(query)`: Performs hybrid FTS5 BM25 + vector search over past completed task episodes across sessions (`episodic.query_episodes(query)`).

2. **Autonomous, Self-Directed Retrieval**:
   - The Supervisor's prompt instructs it to evaluate whether the user's request is self-contained or dependent on prior context.
   - **Self-contained requests** (e.g., "write a python function", "hi", "explain recursion"): The Supervisor executes **zero memory calls**, immediately producing its delegation decision without extra latency.
   - **Context-dependent requests** (e.g., "what is my name?", "remember our discussion?", "continue previous work"): The Supervisor invokes the necessary memory tool(s).

3. **Targeted Context Briefing via Plan**:
   - Upon retrieving memory, the Supervisor synthesizes a concise 1–2 sentence `briefing` (e.g., `"User's name is Alex (from current chat history)"`).
   - The briefing is injected into `state["plan"]` (`Context: <briefing>\nGoal: <reason>`).
   - Specialists read the updated plan within their system prompt (`Current Plan & Context:`), receiving the exact fact needed to address the prompt without being exposed to raw transcripts or database rows.

4. **Session Plumbing**:
   - Updated `AetherState` in `state.py` to include `session_id`, `task_id`, and `context_briefing`.
   - Updated `run_langgraph_task` in `executor.py` and `app.py` to accept and propagate `session_id` into the LangGraph state and configuration.

5. **Resilience & Extraction Refinement**:
   - Refined the guard in `knowledge_graph.py` so concise personal introductions ("My name is ...", "I use Windows") are recorded into long-term memory while pure greetings are ignored.
   - Added an explicit `1.0s` timeout via `asyncio.wait_for` on `litellm.aembedding` in `episodic.py`, guaranteeing immediate fallback to SQLite FTS5 BM25 search when local embedding models (Ollama) are offline.

## Consequences

### Positive
- **Clean Worker Contexts**: Specialists receive zero token bloat, avoiding distractions from irrelevant conversational history.
- **Accurate Recall**: The Supervisor can accurately answer identity and context queries by pulling from the active session, knowledge graph, or episodic store on demand.
- **Zero Latency Overhead on Generic Tasks**: Self-contained coding and reasoning requests bypass memory retrieval completely.
- **Robust Failover**: Hybrid FTS5 BM25 guarantees sub-millisecond episodic search even when neural vector services are unavailable.

### Negative / Trade-offs
- When context retrieval is needed, the Supervisor performs an extra internal LLM tool execution cycle (~0.5–1.5s) before delegating to the specialist.
