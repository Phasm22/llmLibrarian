# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

llmLibrarian indexes **folders you choose** into a local vector store and answers questions from **that index only** — via MCP tools (chunks for the host model) or `pal ask` (retrieve + local Ollama). No built-in chat memory; each query is grounded in indexed files + current question.

**Why it exists (user-facing):** [README.md](README.md), [docs/GUIDE.md](docs/GUIDE.md).  
**Agent operations:** [AGENTS.md](AGENTS.md).  
**Technical contracts:** [docs/TECH.md](docs/TECH.md).

## Development commands

```bash
uv venv && source .venv/bin/activate && uv sync
ollama pull llama3.1:8b
```

```bash
# Engine
llmli add <folder>
llmli ask --in <silo> "query"
llmli ls
llmli inspect <silo>
llmli repair <silo>
llmli repair-ladder
llmli rehydrate [--dry-run]
llmli capabilities

# Operator
pal pull <folder>              # --watch for daemon
pal ask --in <silo> "query"
pal ls --status
pal chroma start               # when LLMLIBRARIAN_CHROMA_HOST is set
pal daemon install|sync|logs
```

## Architecture (map)

**CLIs:** `cli.py` → `llmli`; `pal.py` → `pal` (+ `~/.pal/registry.json` for bookmarks/daemons).

**Pipeline (`src/`):**

- `ingest/` — collect, chunk, extract, Chroma batch add
- `query/` — `intent.py`, `retrieval.py` (hybrid/RRF), `core.py` (`run_ask`, `run_retrieve`)
- `chroma_client.py` — singleton client; HTTP vs embedded; `writer_client` for writes
- `chroma_lock.py` — cross-process flock
- `state.py` — registry, manifest, query health
- `mcp_server.py` — FastMCP tools + HTTP `/healthz`

**Storage:** `LLMLIBRARIAN_DB` (default `./my_brain_db`); collection `llmli`; silo in metadata. Server mode: `chroma run` + `LLMLIBRARIAN_CHROMA_HOST`.

**MCP tools:** `session_context`, `mcp_runtime_status`, `query_personal_knowledge`, `multi_query_knowledge`, `list_silos`, `add_silo`, `trigger_reindex`, `repair_silo`, `health`, … — see [AGENTS.md](AGENTS.md).

**Data flow:**

1. Ingest → chunk → embed → Chroma (+ registry/manifest)
2. Ask → intent route → retrieve → diversify/dedup → optional rerank → Ollama (CLI only)
3. MCP retrieve → chunks JSON only

## Key environment variables

| Variable | Role |
|----------|------|
| `LLMLIBRARIAN_DB` | Persist path |
| `LLMLIBRARIAN_CHROMA_HOST` / `PORT` | HTTP to `chroma run` |
| `LLMLIBRARIAN_MODEL` | Ollama model for ask |
| `LLMLIBRARIAN_RERANK=1` | Cross-encoder rerank (CLI ask) |
| `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION` | MCP embedded reader restart (default on in mcp_server) |

Full list: README “Further reading”, [docs/CHROMA_AND_STACK.md](docs/CHROMA_AND_STACK.md).

## Intent routing (silent)

`LOOKUP`, `EVIDENCE_PROFILE`, `AGGREGATE`, `TAX_QUERY`, `CODE_LANGUAGE`, `CAPABILITIES`, `FILE_LIST`, `STRUCTURE`, `TIMELINE`, … — see `src/query/intent.py` and [docs/TECH.md](docs/TECH.md).

## Chroma safety

Chroma 1.x: **not process-safe** for multiple embedded `PersistentClient` on one path. Production setup: **one** `chroma run`, all clients HTTP. MCP HTTP uses PID file lock (stdio MCP does not). See [docs/CHROMA_AND_STACK.md](docs/CHROMA_AND_STACK.md).
