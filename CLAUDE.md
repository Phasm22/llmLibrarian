# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

llmLibrarian indexes **folders you choose** into a local vector store and answers questions from **that index only** — via MCP tools (chunks for the host model) or `pal ask` (retrieve + local Ollama). No built-in chat memory; each query is grounded in indexed files + current question.

**Why it exists (user-facing):** [README.md](README.md), [docs/GUIDE.md](docs/GUIDE.md).  
**Agent operations:** [AGENTS.md](AGENTS.md) (includes `pc-stacks` host runtime).  
**Technical contracts:** [docs/TECH.md](docs/TECH.md).

**Linux desktop:** stack is cold at login — `pc-stacks up llmlibrarian` before MCP. See [`/home/tj/bin/README.md`](/home/tj/bin/README.md).

## Development commands

```bash
uv venv && source .venv/bin/activate && uv sync
ollama pull llama3.2:latest   # DEFAULT_MODEL; only needed for `pal ask` / `llmli ask`
```

```bash
# Engine
llmli add <folder>
llmli ask --in <silo> "query"
llmli ls                       # Scope column marks private silos
llmli private <silo> [--off]   # exclude a silo from unscoped retrieval
llmli inspect <silo>
llmli repair <silo>
llmli repair-ladder
llmli rehydrate [--dry-run]
llmli capabilities

# Operator
pal pull <folder>              # --watch for daemon
pal ask --in <silo> "query"
pal ls --status
pal private <silo> [--off]     # same flag, operator-side
pal queries                    # audit past MCP queries (--grep/--silo/--since/--summary)
pal chroma install|start|stop|status|logs|uninstall
pal mcp install|start|stop|status|logs|uninstall
pal daemon install|sync|logs|prune-logs|uninstall
pal uninstall [--purge|--purge-data] [--yes]   # tears down mcp+daemon+chroma; keeps DB/env unless --purge*

# macOS status app (SwiftUI, menu bar + window) — see macos/README.md
macos/build.sh --install       # builds macos/build/llmLibrarian.app and replaces /Applications/llmLibrarian.app
```

## Architecture (map)

**CLIs:** `cli.py` → `llmli`; `pal.py` → `pal` (+ `~/.pal/registry.json` for bookmarks/daemons).

**Pipeline (`src/`):**

- `ingest/` — collect, chunk, extract, Chroma batch add
- `query/` — `intent.py`, `retrieval.py` (hybrid/RRF), `core.py` (`run_ask`, `run_retrieve`)
- `chroma_client.py` — singleton client; HTTP vs embedded; `writer_client` for writes
- `chroma_lock.py` — cross-process flock
- `state.py` — silo registry, ingest failures, query health
- `file_registry.py` — file manifest (source of truth for indexed files); the content-hash index is derived from it in memory
- `mcp_server.py` — FastMCP tools + HTTP `/healthz`

**Storage:** `LLMLIBRARIAN_DB` (default `./my_brain_db`); collection `llmli`; silo in metadata. Server mode: `chroma run` + `LLMLIBRARIAN_CHROMA_HOST`.

**MCP tools:** `session_context`, `mcp_runtime_status`, `query_personal_knowledge`, `multi_query_knowledge`, `recent_queries`, `list_silos`, `add_silo`, `set_silo_privacy`, `trigger_reindex`, `repair_silo`, `health`, … — see [AGENTS.md](AGENTS.md).

**Query audit:** every MCP query appends query text + params + per-source-file chunk breakdown to `~/.pal/logs/query-audit.jsonl` (`src/query_audit.py`; disable with `LLMLIBRARIAN_QUERY_AUDIT=0`). Read it via `pal queries` or the `recent_queries` MCP tool. Separate from `usage.log`, which stays a text-free metrics feed for the Argus dashboard.

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

## Machines and silos

**Silos are machine-local.** The Mac and the Linux PC each have their own
`LLMLIBRARIAN_DB`, their own registry, and their own filesystem — a silo on one
does not exist on the other. The Obsidian vault syncs between them; the index
does not, and it should not: the paths would not resolve.

Consequences for an agent working here:

- `list_silos` / `session_context` is the **only** authority on what exists.
  Every registry entry now carries `host`, and `session_context` returns the
  current `host` plus `indexed_by_hosts`.
- A silo named in notes, `log.md`, or memory but absent from `list_silos` is
  **not reachable from this session**. Say "not on this machine" — not "empty",
  and not "the index is broken".
- Never copy a silo roster between machines' docs. Write down the *policy*,
  query for the *roster*.

## Privacy: private silos

Some corpora must not reach a cloud model. That is enforced in code, not by
prompt discipline — discipline failed: an unscoped `multi_query_knowledge`
returned tax and lab-result chunks that nobody asked for.

A silo flagged `private` in the registry is:

- skipped by **unscoped** `query_personal_knowledge`, `multi_query_knowledge`,
  and `find_files` (the Chroma where-clause carries a `$nin`, and the manifest
  readers filter the same set — see `state.private_filter_clause` and
  `file_registry.read_visible_manifest`);
- skipped by automatic scope binding and catalog ranking, so a query can never
  be *implicitly* routed into one;
- still reachable when a caller names it: `silo=<slug>` over MCP, `--in <slug>`
  on the CLI. Naming it is the consent signal.

Unscoped query responses carry `excluded_private_silos` and `privacy_note` so a
thin result reads as "scoped away", not "no such evidence".

Set it with `llmli private <slug>` / `pal private <slug>`, the `set_silo_privacy`
MCP tool, `add_silo(private=True)`, or the lock toggle in the macOS app. Clearing
the flag is the direction that widens exposure, so CLI, MCP, and UI all require
an explicit confirmation for `--off`.

Currently private: `tax-*`, `lab-history-*`. Route questions about them to
`pal ask --in <slug>` rather than pulling values into a cloud context.

## Registry safety

`llmli_registry.json`, `llmli_file_manifest.json`, and pal's bookmark registry
are read-modify-write JSON documents shared by one watcher per silo, the MCP
server, and every `llmli` / `pal` invocation. Writing atomically is not enough —
two processes read the same dict, each edits its copy, and the second write
erases the first.

**Hold `state.registry_transaction(db_path)` across the read *and* the write.**
`src/registry_lock.py` implements it (flock on `<file>.lock`, backoff, 30s
budget via `LLMLIBRARIAN_REGISTRY_LOCK_TIMEOUT_SECONDS`, reentrant per thread).
It deliberately does **not** reuse `chroma_lock.py`: that one is a no-op in HTTP
mode, correct for Chroma's HNSW and wrong for a JSON file on local disk.

A read must never mutate. `pal._read_llmli_registry` used to run
`cleanup_stale_registry_entries` on every read; once paths were canonicalized
through symlinks, that deleted a live silo's entry and orphaned 2467 chunks.
Cleanup is now opt-in (`cleanup=True`) and runs from `pal daemon sync`, and it
only removes pre-migration hash-less slugs.

## Chroma safety

Chroma 1.x: **not process-safe** for multiple embedded `PersistentClient` on one path. Production setup: **one** `chroma run`, all clients HTTP. MCP HTTP uses PID file lock (stdio MCP does not). See [docs/CHROMA_AND_STACK.md](docs/CHROMA_AND_STACK.md).
