# AGENTS

Primary source of truth for coding agents in this repo.

If any other document conflicts with this file, follow `AGENTS.md`.

## What this project is (read first)

llmLibrarian is a **local personal knowledge index**: folders → chunks in Chroma → retrieval tools for assistants or CLI. It is **not** a chat product; it is a **context engine** with deterministic ingest, registry/manifest state, and observable repair paths.

**Human story:** [README.md](../README.md) and [docs/GUIDE.md](docs/GUIDE.md).  
**Contracts:** [docs/TECH.md](docs/TECH.md), [docs/orchestration-matrix.md](docs/orchestration-matrix.md).

Agent priorities:

1. Keep behavior **deterministic and observable** (no hidden memory, no prompt-only routing flags).
2. Prefer **MCP tools** when the HTTP MCP server is available; otherwise `pal` then `llmli`.
3. Keep docs and tests aligned with **current** tool names and command behavior.

## MCP tools (actual names)

Do not use outdated names like `retrieve` / `retrieve_bulk`. Current surface:

| Tool | Use |
|------|-----|
| `health` | DB, chroma transport, query health, HNSW audit |
| `list_silos` | Slugs, chunk counts, staleness (`check_staleness=True`) |
| `capabilities` | Supported file types |
| `query_personal_knowledge` | Primary retrieval → chunks (no LLM in MCP) |
| `multi_query_knowledge` | Parallel queries, merged chunks |
| `explain_retrieval` | Debug hybrid/vector signals |
| `find_files` | Manifest-only path/date search |
| `add_silo` | Index path (`confirm=True`) |
| `trigger_reindex` | Incremental reindex (`confirm=True`; **not** right after `add_silo`) |
| `repair_silo` | Hard wipe + re-index silo |
| `update_file` / `remove_file` | Single-file maintenance |
| `watch_coverage` | Read-only daemon/bookmark diagnostics |
| `inspect_silo` | Per-file chunk counts |
| `session_context` | One-call bootstrap: roster + compact health + recommended actions |
| `mcp_runtime_status` | MCP/chroma runtime visibility (pid lock, process count, transport) |

MCP returns **chunks**; the host model answers. Local synthesis: `pal ask` / `llmli ask` (Ollama).
MCP tool docstrings follow: **Use when / Do not use when / Pairs with**.

## Session-start checklist (MCP)

1. Prefer `session_context(check_staleness=True)` before retrieval.
2. If `is_stale: true` and `stale_file_count` is substantial → `trigger_reindex` before querying.
3. If `stale_file_count` is small (≤2–3) **and** `newest_source_mtime_iso` matches the silo `updated` timestamp → treat as index race noise; skip reindex.
4. `db_exists: false` → fix `LLMLIBRARIAN_DB` in the MCP launch env before any retrieval.
5. `health()` → `chroma_transport`: if `embedded` and MCP is up, CLI ingest may be blocked; use server mode (`LLMLIBRARIAN_CHROMA_HOST`, `pal chroma start`) — [docs/CHROMA_AND_STACK.md](docs/CHROMA_AND_STACK.md).

If retrieval returns **zero chunks** with no `error`, do **not** assume the KB is empty. Cross-check `session_context`/`list_silos` (`chunks_count`, `has_index_errors`, `has_ingest_failures`) and call `health`.

Use **`pal`** / **`llmli`** when MCP is unavailable or for flags not on tools (repair-ladder, rehydrate, trace, etc.).

**Orchestration:** [docs/orchestration-matrix.md](docs/orchestration-matrix.md). Ingest: `orchestration.ingest.run_ingest` → `ingest.run_add`.

## Canonical workflows

### Setup

```bash
uv venv && source .venv/bin/activate && uv sync
```

### Ingest

```bash
pal pull /path/to/folder
# llmli add /path/to/folder
```

With MCP + watchers: prefer server mode; writes via `add_silo` / `trigger_reindex` when MCP holds the DB.

### Query

```bash
pal ask --in <silo> "question"
pal ask --unified "question"
```

### Diagnose

```bash
pal ls --status
pal inspect <silo> --top 20
llmli repair-ladder
llmli rehydrate --dry-run
```

### Test

```bash
uv run pytest -q tests/unit
```

## Command notes

- `pal` — operator CLI (pull, ask, ls, daemon, chroma service).
- `llmli` — engine CLI (scripting, repair, rehydrate, log).
- `pal sync` — refresh dev self-silo `__self__` when needed.
- **Claude Desktop MCP (.mcpb):** after `mcp_server.py` changes, `pal extension pack` when `LLMLIBRARIAN_MCP_PACK_CMD` is set; stdio via `.mcp.json` does not update the Desktop binary. `pal ls --status` / `pal sync` warn on stale pack hash.
- `pal ask in <silo> "..."` → normalized to `--in`.

## Documentation policy

- User-facing narrative: README + [docs/GUIDE.md](docs/GUIDE.md).
- Behavior contracts: TECH, orchestration-matrix, CHROMA_AND_STACK.
- Remove stale implementation diaries from active guidance; do not resurrect one-off handoffs in AGENTS.

## Agent guardrails

- Run unit tests for touched behavior before finishing.
- Do not add broad config surfaces unless required.
- Keep changes focused and reversible.
- Do not call `trigger_reindex` immediately after `add_silo` (documented race / lock hazard).
