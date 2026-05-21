# Chroma and Stack Notes (Current)

## Current Stack

- Vector store: ChromaDB (persistent local collection, or HTTP to `chroma run`)
- Ingest: local file processors + chunking + metadata registry/manifest
- Query: intent routing + deterministic guardrails + retrieval + optional LLM fallback
- CLI: `pal` (operator), `llmli` (direct)

## Chroma concurrency (important)

ChromaDB 1.x is **thread-safe but not process-safe** for embedded `PersistentClient` on one `persist_directory`. Two processes each opening `PersistentClient` on the same path can **SIGSEGV** in native HNSW code (not a Python lock error).

### Embedded mode (default)

- One long-lived reader (MCP HTTP, `pal pull --watch`) plus a separate `pal pull` / `llmli add` writer is unsafe.
- llmLibrarian blocks embedded writes when MCP `/healthz` or an active watch process is detected (`preflight_embedded_write`).
- Long-lived readers use `.llmli_chroma_generation` + optional `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION=1` to restart after external writes.

**While MCP is up:** use MCP `add_silo` / `trigger_reindex`, or stop MCP before `pal pull`.

### Server mode (recommended for MCP + CLI together)

Run a single local Chroma server; all clients use `HttpClient`:

```bash
pal chroma install && pal chroma start
```

In `.env.mcp` (and watch daemon env via `pal daemon sync`):

```bash
LLMLIBRARIAN_CHROMA_HOST=127.0.0.1
LLMLIBRARIAN_CHROMA_PORT=8000
```

Same `LLMLIBRARIAN_DB` path is passed to `chroma run --path`. No DB migration.

## Environment

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_DB` | Persist directory (embedded path and `chroma run --path`) |
| `LLMLIBRARIAN_CHROMA_HOST` | If set, use HTTP client instead of embedded |
| `LLMLIBRARIAN_CHROMA_PORT` | Chroma server port (default `8000`) |
| `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION` | Embedded readers exit 99 after external write |
| `LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT` | Tests only; disable embedded write guard |

## See also

- [orchestration-matrix.md](./orchestration-matrix.md) — entry points and locks
- [AGENTS.md](../AGENTS.md) — agent session checklist
