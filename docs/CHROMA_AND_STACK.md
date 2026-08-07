# Chroma and Stack Notes (Current)

Operator-focused notes for **one Chroma process on disk** and many HTTP clients (MCP, `pal`, watchers). User-facing “why” and workflows: [GUIDE.md](./GUIDE.md).

## Current Stack

- Vector store: ChromaDB (persistent local collection, or HTTP to `chroma run`)
- Ingest: local file processors + chunking + metadata registry/manifest
- Query: intent routing + deterministic guardrails + retrieval + optional LLM fallback (CLI `ask` only; MCP returns chunks)
- CLI: `pal` (operator), `llmli` (direct)

## The supported deployment

**One answer, so the rest of this file is not a menu:**

| Situation | Chroma mode | MCP transport |
|-----------|-------------|---------------|
| A service runs (MCP HTTP, `pal pull --watch`, daemon) | **server** (`chroma run` + `LLMLIBRARIAN_CHROMA_HOST`) | **streamable-http** |
| No service; one CLI process at a time | embedded | n/a |
| Claude Desktop `.mcpb` extension | embedded | stdio |

The HTTP MCP service plus `chroma run` is **the supported deployment**. Embedded
mode is correct only when nothing else holds the index open. stdio exists for
the Claude Desktop extension: it is single-process, takes no PID lock, and must
not run alongside `pal pull` / `llmli add` on the same `LLMLIBRARIAN_DB`.

Ask the running server rather than inferring from config on disk:

```bash
curl -s localhost:8765/healthz
```

It reports `transport` and `chroma_transport`, which is what a second process
needs in order to decide whether writing directly is safe.

## Chroma concurrency (important)

ChromaDB 1.x is **thread-safe but not process-safe** for embedded `PersistentClient` on one `persist_directory`. Two processes each opening `PersistentClient` on the same path can **SIGSEGV** in native HNSW code (not a Python lock error). The same hazard applies to two clients inside one process, which is why `get_client()` caches by resolved path.

### Embedded mode

- One long-lived reader (stdio MCP, `pal pull --watch`) plus a separate `pal pull` / `llmli add` writer is unsafe.
- llmLibrarian refuses embedded writes when MCP `/healthz` or an active watch process is detected (`preflight_embedded_write`). If `/healthz` answers but rejects our credentials, the write is also refused — a live MCP server that cannot be identified is not evidence that it is safe to proceed. Set `LLMLIBRARIAN_MCP_AUTH_TOKEN` so the probe can authenticate.
- Long-lived readers track `.llmli_chroma_generation`. When another process writes, a reader either exits 99 for its supervisor to restart (`LLMLIBRARIAN_EXIT_ON_STALE_GENERATION`, which `mcp_server` **defaults to on**) or drops and reopens its cached client. A stale client is never returned to a caller.

**While MCP is up:** use MCP `add_silo` / `trigger_reindex`, or stop MCP before `pal pull`.

### Server mode

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

## Running the MCP HTTP server

```bash
cp .env.mcp.example .env.mcp   # then edit LLMLIBRARIAN_DB
./scripts/run_mcp_http.sh
```

As a supervised service: `pal install --mcp`. Remote exposure and the auth
model: [MCP_TAILSCALE_FUNNEL.md](./MCP_TAILSCALE_FUNNEL.md).

## Environment

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_DB` | Persist directory (embedded path and `chroma run --path`) |
| `LLMLIBRARIAN_CHROMA_HOST` | If set, use HTTP client instead of embedded |
| `LLMLIBRARIAN_CHROMA_PORT` | Chroma server port (default `8000`) |
| `LLMLIBRARIAN_CHROMA_SSL` | Use HTTPS for the Chroma client |
| `LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC` | Min seconds between cached-client heartbeats (default `5`) |
| `LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS` | Wait budget for the persist-dir flock and the MCP mutex (default `10`; `0`/`off` = block indefinitely) |
| `LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS` | Overrides the above for the MCP in-process mutex only |
| `LLMLIBRARIAN_MIN_FREE_BYTES` | Storage preflight floor (default 512 MiB) |
| `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION` | Embedded readers exit 99 after an external write (default **on** in `mcp_server`) |
| `LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT` | Tests only; disable embedded write guard |
| `LLMLIBRARIAN_MCP_TRANSPORT` | `stdio` (default) or `streamable-http` |
| `LLMLIBRARIAN_MCP_HOST` / `_PORT` / `_PATH` | MCP HTTP bind address and route |
| `LLMLIBRARIAN_MCP_REQUIRE_AUTH` | Require a static bearer token on HTTP transports |
| `LLMLIBRARIAN_MCP_AUTH_TOKEN` | The bearer token, used by both the server and the `/healthz` probe |

Boolean variables accept `1`, `true`, `yes`, or `on` (case-insensitive).

## See also

- [orchestration-matrix.md](./orchestration-matrix.md) — entry points and locks
- [MCP_TAILSCALE_FUNNEL.md](./MCP_TAILSCALE_FUNNEL.md) — HTTP serving and remote exposure
- [AGENTS.md](../AGENTS.md) — agent session checklist
