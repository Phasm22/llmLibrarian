# ChromaDB utilization & stack options

## Are we utilizing ChromaDB enough?

**Short answer: yes for the current design.** We use it well for a minimal RAG stack.

| Feature | We use it? | Where |
|--------|------------|--------|
| Persistent client, one collection | ✅ | `query_engine`, `ingest`, `cli` |
| `collection.query(query_texts, n_results, where)` | ✅ | `run_ask`: vector search + silo filter |
| `collection.add(ids, documents, metadatas)` in batches | ✅ | `ingest._batch_add` |
| `collection.delete(where={"silo": ...})` | ✅ | `ingest` (replace silo), `cli rm` |
| `collection.get(where=..., include=["metadatas"])` | ✅ | `cli inspect` (per-file chunk counts) |
| **Metadata filters** `$eq`, `$in`, `$and`, `$or`, `$gt`/`$gte` | ✅ | `run_ask`: silo filter; catalog sub-scope uses `$in` on `source` to restrict to path set from file registry |
| **`where_document`** (full-text: `$contains`, `$regex`) | ✅ | EVIDENCE_PROFILE hybrid: get() + RRF (see below) |
| `client.delete_collection` | ✅ | Ingest archetype rebuild |
| Update/upsert by id | ❌ | We delete + re-add; fine for replace-by-silo |

So we’re in good shape. The one Chroma feature that would directly improve retrieval is **`where_document`** for EVIDENCE_PROFILE (lexical pre-filter or second query fused with vector).

---

## Use cases for the tools you listed

### Vector DB: Qdrant or Chroma

- **Chroma** — We’re on it. Fits local-first, simple API, good enough scale for personal/single-user.
- **Qdrant** — Use if you need: bigger scale, richer payload filters, or hybrid (BM25 + vector) in one engine. No need to switch for current scope.

**Verdict:** Stay on Chroma; consider Qdrant only if you outgrow it.

---

### RAG index: LlamaIndex or Haystack

- **LlamaIndex / Haystack** — Query pipelines, document loaders, node parsers, response synthesis, multiple retrievers (BM25, hybrid).
- **Our stack** — Custom pipeline: ingest → Chroma; `run_ask` = intent → retrieve → diversify → optional rerank → prompt → Ollama. No framework.

**Use case for LlamaIndex/Haystack:**  
If you want to standardize on a framework and add more retriever types (e.g. BM25, hybrid) without hand-rolling. That’s a larger refactor. For “keep the compiler, no new flags,” staying framework-free is consistent.

**Verdict:** Optional. Add only if you want to lean on a standard RAG abstraction and built-in hybrid/BM25.

---

### Structured outputs (JSON schemas + functions)

- **Today** — LLM returns a single string; we don’t parse JSON or tool calls.
- **Use case** — If you want the model to return e.g. `{"answer": "...", "sources": [{"path": "...", "quote": "..."}]}` or tool calls, you’d use Ollama’s structured output / JSON mode and parse in code. Enables stricter parsing and tool use.

**Verdict:** Optional enhancement; not required for current “print answer + sources” UX.

---

### Trace logging (local file + structured events)

- **Implemented.** Set **`LLMLIBRARIAN_TRACE`** to a file path; each `run_ask` appends one JSON line: `intent`, `n_stage1`, `n_results`, `model`, `silo`, `source_label`, `num_docs`, `time_ms`, `query_len`, `hybrid`. No CLI change.

---

### Semantic Kernel / LangChain

- **Orchestration and chain/agent patterns.** We deliberately avoided agent loops; we have a compiler (`run_ask`) and deterministic pipelines.
- **Use case** — If you later want multi-step tools (e.g. “search → summarize → write to file”) or pluggable tools, SK or LangChain could help. Not needed for current “one command, no flags” design.

**Verdict:** Skip for now; add only if you introduce explicit multi-step tools.

---

### Hybrid search (lexical + embeddings)

- **Implemented.** For **EVIDENCE_PROFILE** when querying a silo (`llmli ask --in <silo>`): (1) vector query with `n_stage1`; (2) Chroma **`get(where_document={"$or": [{"$contains": "I like"}, ...]}, where={"silo": silo})`**; (3) **RRF** merge of the two result sets; (4) continue with rerank/diversify. No CLI change. If lexical returns no chunks, fallback to in-app trigger reorder.
- **True BM25** — Chroma OSS doesn’t do BM25; optional future: in-memory BM25 over chunks + RRF.

---

## Summary

| Tool / area | Use now? | Recommendation |
|-------------|----------|----------------|
| **Chroma** | Yes | Keep; **`where_document`** used for EVIDENCE_PROFILE hybrid. |
| **Qdrant** | No | Only if you outgrow Chroma (scale, native hybrid). |
| **LlamaIndex / Haystack** | No | Optional if you want a full RAG framework; not required. |
| **Structured outputs** | No | Optional for strict JSON/tool output. |
| **Trace logging** | Yes | Set **`LLMLIBRARIAN_TRACE`** to a file path for JSON-lines trace. |
| **Semantic Kernel / LangChain** | No | Skip unless you add multi-step tools. |
| **Hybrid search** | Yes | EVIDENCE_PROFILE: Chroma `where_document` + RRF; no CLI change. |

Trace and hybrid are implemented; no DB schema change and no new CLI flags.
