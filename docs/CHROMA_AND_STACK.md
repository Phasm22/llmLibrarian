# Chroma and Stack Notes (Current)

Operator-focused notes for **one Chroma process on disk** and many HTTP clients (MCP, `pal`, watchers). User-facing “why” and workflows: [GUIDE.md](./GUIDE.md).

## Current Stack

- Vector store: ChromaDB (persistent local collection, or HTTP to `chroma run`)
- Ingest: local file processors + chunking + metadata registry/manifest
- Query: intent routing + deterministic guardrails + retrieval + optional LLM fallback (CLI `ask` only; MCP returns chunks)
- CLI: `pal` (operator), `llmli` (direct)

## Chroma concurrency (important)

ChromaDB 1.x is **thread-safe but not process-safe** for embedded `PersistentClient` on one `persist_directory`. Two processes each opening `PersistentClient` on the same path can **SIGSEGV** in native HNSW code (not a Python lock error).

### Embedded mode (zero-config; safe only when nothing else holds the index)

- One long-lived reader (MCP, `pal pull --watch`) plus a separate `pal pull` / `llmli add` writer is unsafe.
- llmLibrarian refuses embedded writes when MCP `/healthz` or an active watch process is detected (`preflight_embedded_write`). If `/healthz` answers but rejects our credentials, the write is also refused: a live MCP server that cannot be identified is not evidence it is safe to proceed. Set `LLMLIBRARIAN_MCP_AUTH_TOKEN` so the probe can authenticate, or `LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT=1` if you know it holds a different DB.
- Long-lived readers track `.llmli_chroma_generation`. When another process writes, a reader either exits 99 for its supervisor to restart it (`LLMLIBRARIAN_EXIT_ON_STALE_GENERATION`, which `mcp_server` **defaults to on**) or drops and reopens its cached client. A stale client is never returned to a caller.

**While MCP is up:** use MCP `add_silo` / `trigger_reindex`, or stop MCP before `pal pull`.

### Server mode (the supported deployment)

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

**Every client on that path must be HTTP.** Any client missing `LLMLIBRARIAN_CHROMA_HOST` / `LLMLIBRARIAN_CHROMA_PORT` opens an embedded `PersistentClient` on the path the server owns — the exact concurrent-client hazard server mode exists to remove.

**One MCP process, reached over HTTP.** The plugin no longer ships a stdio `.mcp.json`: a stdio entry spawns a *new* `mcp_server.py` per client, so a desktop session, a phone session, and a local checkout each got their own process against one Chroma path — and a bug reproduced in one told you nothing about the others. The supported topology is a single `llmlibrarian-mcp.service` on `LLMLIBRARIAN_MCP_PORT` that every client connects to. Point Claude Code at it with an `http` MCP entry in your **local** config (the URL embeds `LLMLIBRARIAN_MCP_PATH`, so it must not be committed). After changing `mcp_server.py`, run `pc-stacks redeploy llmlibrarian`.

The repo's root `.mcp.json` is still a stdio entry, so a checkout opened in Claude Code spawns its own `mcp_server.py` alongside the service. That is tolerable only because it now sets `LLMLIBRARIAN_CHROMA_HOST`/`PORT`: every one of those processes is an HTTP client of the single `chroma run`, not a second embedded writer. Drop those two variables and the same setup becomes the corruption case. `mcp_runtime_status` counts live `mcp_server.py` processes if you need to confirm what is actually running.

#### Lock contention & query availability

Cross-process access is coordinated by an advisory `flock` (`src/chroma_lock.py`): reads take a **shared** lock, writes an **exclusive** one. In embedded mode this is required — it prevents the concurrent-`PersistentClient` SIGSEGV — but it means an in-progress index write blocks all queries until it finishes, and a query that waits longer than `LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS` (default 5s) fails with a lock-timeout.

Mitigations that keep contention from surfacing as unavailability:

- **Both locks are skipped in server mode.** `chroma run` is the only process touching the persist directory and orders access itself, so the `flock` protects nothing there — it only serializes llmLibrarian's own clients against each other. Skipping the shared lock stops queries blocking behind an index write; skipping the exclusive lock stops a `llmli add` failing outright because a peer index was mid-flight. Force either back with `LLMLIBRARIAN_CHROMA_SHARED_LOCK=1` / `LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK=1`. In embedded mode both are always taken.
- **Writers wait longer than readers.** A reader blocked 5s looks hung to its caller; a queued `llmli add` has nothing better to do than wait. Writers default to 120s (`LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS`), readers stay at 5s.
- **Waiters back off.** Lock polling grows 20ms → 500ms instead of a fixed 100ms tick, so contending processes stop retrying in lockstep. The final sleep is clamped to the remaining budget.
- **MCP reads skip the in-process mutex in server mode.** `mcp_server._chroma_lock` exists because two threads driving one *embedded* `PersistentClient` into the Rust HNSW writer once grew `link_lists.bin` to 680 GB. Under `chroma run` no thread touches HNSW, so the mutex only made every MCP read return `busy` for the full duration of a watcher-triggered background reindex. Reads now skip it in HTTP mode; writes (`repair_silo`, `update_file`, `remove_file`, and the background reindex write phase) still take it. Restore with `LLMLIBRARIAN_MCP_READ_LOCK=1`.

#### Transport retry (HTTP mode)

`chroma run` is unavailable for roughly **0.5s** during a restart (`pc-stacks redeploy`, an OOM kill against `MemoryMax=6G`, a systemd restart). `src/chroma_client.py` retries connection-level failures — 3 attempts, ~0.7s of backoff total, never application errors, and a no-op in embedded mode. Writes replay only when the request provably never reached the server (`ConnectError`/`ECONNREFUSED`); a read timeout may mean the write landed and is still applying. Tune with `LLMLIBRARIAN_CHROMA_HTTP_RETRIES` (0 disables).

The point is the MCP caller: an unretried blip reaches Claude as a tool error, and a model may read "tool failed" as "the knowledge base has nothing" and answer from training data instead — the same class of silent-wrong-answer as an unflagged rebuild window. Note this is defensive: no end-to-end reproduction of a failure it prevents has been captured, partly because chromadb retains its system/transport cache across `HttpClient()` construction within a process.

#### Rebuild visibility

A `--full` rebuild deletes a silo's rows before writing replacements, so a query landing in that window gets zero chunks and no error — indistinguishable from "the source doesn't say that." Three things close it: the delete is deferred until the replacements are embedded and in hand; the write-ahead marker (`src/ingest_journal.py`) records `kind` and `pid` and is held until a vector probe against the silo succeeds (Chroma keeps building HNSW after the write returns); and `run_retrieve` samples that marker either side of retrieval and tags the response with `write_in_progress` + `retryable` + a coverage note. Read tools surface it, so a remote session can tell "rebuilding" from "absent."
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

Boolean variables accept `1`, `true`, `yes`, or `on` (case-insensitive).

**Storage and transport**

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_DB` | Persist directory (embedded path and `chroma run --path`) |
| `LLMLIBRARIAN_CHROMA_HOST` | If set, use HTTP client instead of embedded |
| `LLMLIBRARIAN_CHROMA_PORT` | Chroma server port (default `8000`) |
| `LLMLIBRARIAN_CHROMA_SSL` | Use HTTPS for the Chroma client |
| `LLMLIBRARIAN_CHROMA_HTTP_RETRIES` | Connection-level retry attempts in HTTP mode (default `3`; `0` disables) |
| `LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC` | Min seconds between cached-client heartbeats (default `5`) |

**Locking**

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS` | Max wait for a Chroma lock (default `5` read; `0`/`off`/`none` = block indefinitely). Read by *both* the flock layer and the MCP in-process mutex, so the sentinel means the same thing everywhere. |
| `LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS` | Writer-only override (default `120`) — a queued `llmli add` can afford to wait where a reader cannot |
| `LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS` | Override for the MCP in-process mutex only; falls through to the shared var |
| `LLMLIBRARIAN_CHROMA_SHARED_LOCK` | Force the shared read flock even in server mode (default off — read lock skipped in HTTP mode) |
| `LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK` | Force the exclusive write flock even in server mode |
| `LLMLIBRARIAN_MCP_READ_LOCK` | Force MCP reads to take the in-process mutex in server mode |

**MCP serving**

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_MCP_TRANSPORT` | `stdio` (default) or `streamable-http` |
| `LLMLIBRARIAN_MCP_HOST` / `_PORT` / `_PATH` | Bind address and route for the HTTP service |
| `LLMLIBRARIAN_MCP_REQUIRE_AUTH` | Require a static bearer token on HTTP transports |
| `LLMLIBRARIAN_MCP_AUTH_TOKEN` | The bearer token. Used by the server *and* by the embedded-write guard's `/healthz` probe — without it that guard cannot identify an authenticated server. |
| `LLMLIBRARIAN_MCP_BEARER_TOKEN` | Older client-side spelling, still written by `pal`; read as a fallback |
| `LLMLIBRARIAN_MCP_URL` | Full MCP endpoint for `pal`'s client, overriding host/port/path |

**Recovery / testing**

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION` | Embedded readers exit 99 after an external write (default **on** in `mcp_server`); when off, the cached client is dropped and reopened instead |
| `LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT` | Tests only; disable embedded write guard |

## See also

- [orchestration-matrix.md](./orchestration-matrix.md) — entry points and locks
- [AGENTS.md](../AGENTS.md) — agent session checklist
