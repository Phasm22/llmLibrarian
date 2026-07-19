# Chroma and Stack Notes (Current)

Operator-focused notes for **one Chroma process on disk** and many HTTP clients (MCP, `pal`, watchers). User-facing “why” and workflows: [GUIDE.md](./GUIDE.md).

## Current Stack

- Vector store: ChromaDB (persistent local collection, or HTTP to `chroma run`)
- Ingest: local file processors + chunking + metadata registry/manifest
- Query: intent routing + deterministic guardrails + retrieval + optional LLM fallback (CLI `ask` only; MCP returns chunks)
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

#### Lock contention & query availability

Cross-process access is coordinated by an advisory `flock` (`src/chroma_lock.py`): reads take a **shared** lock, writes an **exclusive** one. In embedded mode this is required — it prevents the concurrent-`PersistentClient` SIGSEGV — but it means an in-progress index write blocks all queries until it finishes, and a query that waits longer than `LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS` (default 10s) fails with a lock-timeout.

Two mitigations keep contention from surfacing as unavailability:

- **Shared read lock is skipped in server mode.** The single `chroma run` process is the only on-disk reader/writer and serializes safely, so the redundant read `flock` is dropped — queries no longer block behind an in-progress index write. Force the old behavior with `LLMLIBRARIAN_CHROMA_SHARED_LOCK=1`. (Exclusive write locks are always taken.)
- **Read tools degrade to `busy`, not error.** On a lock timeout, `query_personal_knowledge` / `multi_query_knowledge` / `explain_retrieval` / `find_files` / `inspect_silo` return `{"busy": true, "retryable": true, "retry_after_seconds": N}` instead of a hard error — the caller should retry rather than treat the index as broken or empty.

The transient "DB lock — startup contention" you may see right after starting a server or a test run is a writer briefly holding the exclusive lock; it clears on its own.

## Local runtime (on-demand / `pc-stacks`)

On TJ's Linux desktop, Chroma + MCP + watch daemons are **cold by default** at login.

```bash
pc-stacks up llmlibrarian   # start chroma → mcp → watchers
pc-stacks status            # verify warm before MCP session
```

Do **not** assume `:8000` / `:8765` are up because systemd units are installed — they are disabled at boot. Agents: see [AGENTS.md](../AGENTS.md#host-runtime-pc-stacks) and [`/home/tj/bin/README.md`](/home/tj/bin/README.md).

Traceability: **PC Idle Quietdown** plan (Cursor plans, Jul 2025).

## Environment

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_DB` | Persist directory (embedded path and `chroma run --path`) |
| `LLMLIBRARIAN_CHROMA_HOST` | If set, use HTTP client instead of embedded |
| `LLMLIBRARIAN_CHROMA_PORT` | Chroma server port (default `8000`) |
| `LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS` | Max wait for the Chroma flock before a busy/timeout (default `10`; `0`/`off` = block forever) |
| `LLMLIBRARIAN_CHROMA_SHARED_LOCK` | Force the shared read flock even in server mode (default off — read lock skipped in HTTP mode) |
| `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION` | Embedded readers exit 99 after external write |
| `LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT` | Tests only; disable embedded write guard |

## See also

- [orchestration-matrix.md](./orchestration-matrix.md) — entry points and locks
- [AGENTS.md](../AGENTS.md) — agent session checklist
