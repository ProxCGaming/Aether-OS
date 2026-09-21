# ADR-0020: Hybrid Retrieval with Local Embeddings

## Status
Proposed

## Context
Aether currently uses SQLite FTS5 for full-text search in the `episodic.py` memory layer. While keyword search is effective for exact matches, it fails to capture semantic meaning, making it difficult to retrieve relevant past episodes when the terminology differs. 
To improve recall and agent reasoning, we need to introduce semantic search capabilities alongside the existing FTS5 index.

## Decision
We will implement a Hybrid Retrieval system using Local Embeddings, with the following architectural choices:

1. **Local Embeddings with SentenceTransformers**:
   We will use `sentence-transformers/all-MiniLM-L6-v2` (or a similar lightweight model) via the `sentence-transformers` library to generate embeddings locally. This avoids external API dependencies for memory and ensures privacy.

2. **Separate Vector Database (`embeddings.db`)**:
   Instead of forcing vector data into the relational SQLite database (`aether_memory.db`), we will create a dedicated `embeddings.db` (using a lightweight vector store like `chromadb` or just a simple sqlite table if chroma is too heavy, but for simplicity, `chromadb` local or a secondary sqlite table with numpy arrays is preferred. Given minimal dependencies, we will use a separate SQLite table/db for raw vectors, and compute cosine similarity in-memory for the first iteration, or use `chromadb`). Actually, to keep dependencies minimal, we will store embeddings as blob arrays in a new SQLite database (`embeddings.db`) and compute similarity in memory or via a simple extension.

3. **Asynchronous Memory Operations**:
   Embedding generation is blocking/CPU-bound. To prevent freezing the main FastAPI event loop, `store_episode()` and `query_episodes()` will be made `async`. The embedding extraction will be offloaded to a background thread/pool.

4. **Reciprocal Rank Fusion (RRF)**:
   We will combine the results from the FTS5 keyword search and the semantic vector search using Reciprocal Rank Fusion. RRF normalizes ranks from multiple sources without needing to calibrate raw similarity scores.

## Consequences
- **Positive**: Significantly better retrieval recall. Agents will find relevant context even with different phrasing.
- **Positive**: Maintains local-first privacy.
- **Negative**: Increased CPU usage and memory footprint for loading the embedding model.
- **Negative**: `supervisor.py` and other call sites of episodic memory must be refactored to `await` the async memory methods.

## Implementation Steps
1. Add `sentence-transformers` to dependencies (or use `litellm` if we decide to rely on an external provider, but the prompt mandates local embeddings).
2. Create `embeddings.db` init and `_embed_text`, `_cosine_similarity` helpers.
3. Refactor `store_episode()` to be async and generate/store embeddings.
4. Refactor `query_episodes()` to be async, query both FTS5 and embeddings, and apply RRF.
5. Update `supervisor.py` to `await` these memory operations.
