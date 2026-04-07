# CLI / MCP orchestration matrix

Single reference for how ingest and recovery behave across entry points. Core implementation: [`run_add`](../src/ingest/__init__.py). Shared orchestration wrapper: [`run_ingest`](../src/orchestration/ingest.py) (`orchestration.ingest`).

## Dimensions

| Dimension | Notes |
|-----------|--------|
| **Entry point** | `llmli add`, `pal pull <path>`, `pal pull` (all bookmarks), `pal sync` / `ensure_self_silo` (`__self__`), MCP `add_silo` / `trigger_reindex` / `repair_silo` |
| **Path kind** | Directory (default include/exclude rules) vs **single file** (bypasses include/exclude; e.g. `places.sqlite`) |
| **Cloud roots** | Blocked unless `allow_cloud` / `--allow-cloud` |
| **Concurrency** | Cross-process: [`chroma_lock`](../src/chroma_lock.py) (flock). MCP: in-process `_chroma_lock` around writes. Only one heavy indexer should run per DB. |
| **Observability** | `pal pull` (all): streams child `llmli add` stderr/stdout; sets no `LLMLIBRARIAN_QUIET` for the child. Single-path `pal pull`: in-process logs. Optional `LLMLIBRARIAN_STATUS_FILE` JSON at end of `run_add`. |
| **Performance** | `workers`, `embedding_workers`; Apple Silicon MPS forces single embedding thread unless large-ingest CPU policy applies ([`embeddings.py`](../src/embeddings.py)). |
| **Recovery** | `llmli repair` / MCP `repair_silo`: hard reset silo chunks + re-index. `trigger_reindex` / incremental `add`: crawl changed files. Killing mid-run can leave partial chunks; re-run add or repair. |

## Matrix (ingest / add)

| Capability | `llmli add` | `pal pull <path>` | `pal pull` (all) | MCP `add_silo` |
|------------|-------------|-------------------|------------------|----------------|
| Directory | Yes | Yes | Yes (per bookmark) | Yes |
| Single file | Yes | Yes | Yes (per path) | Yes (via `run_ingest`; same as `run_add`) |
| `--full` / incremental | `--full` | `--full` | `--full` | `full=True` |
| Cloud paths | `--allow-cloud` | `--allow-cloud` | `--allow-cloud` | `allow_cloud=` |
| Workers | `--workers`, `--embedding-workers` | same | same | not exposed (defaults) |
| Forced silo slug | hidden `--silo` | N/A | N/A | `silo=` |
| Status JSON for parent | via `LLMLIBRARIAN_STATUS_FILE` env | subprocess can set | `pull_all` sets temp status file | N/A |
| Dev self-silo (`__self__`) | N/A | via `ensure_self_silo` → `run_ingest` | N/A | N/A |

**Footguns**

- Two concurrent `add`/`pull` processes on the **same** DB: second may block on Chroma lock or corrupt if bypassed—serialize.
- Directory crawl **excludes** `*.sqlite` / paths containing `Firefox`; single-file `add` bypasses that.
- MCP `repair_silo` / `trigger_reindex`: see tool docs; re-crawl drops missing files from disk.
- After a **doc_type metadata** change in the engine, re-index affected silos (`pal pull`, `trigger_reindex`, or `repair_silo`) so existing chunks pick up updated `doc_type` tags.

## Git history themes (representative)

Recent work clustered by area (see `git log`):

| Theme | Example commits |
|-------|-------------------|
| Chroma / reliability | Single client, atomic ingest, 1.5.5 upgrade, SIGSEGV fix, locks |
| MCP | Serialization, retrieval, `add_silo`, health/diagnostics |
| Ingest / processors | SQLite/Firefox bookmarks, PDF tables, OCR, images |
| pal | Pull watch, registry, bookmarks batch |
| macOS | MPS vs CPU embedding policy, hardware notes |

Use `git log -G 'run_add|pull_all|add_silo|chroma_lock'` for mechanical hotspots.

## MCP vs CLI gaps (closed / open)

| Item | Status |
|------|--------|
| `add_silo` single-file parity with `run_add` | Closed: accepts file paths like `llmli add`. |
| Every `llmli` flag on MCP | Open: add flags only as agents need them (`workers`, etc.). |
| Streaming progress over MCP | Open: ingest remains synchronous; use `health` / `inspect_silo` after. |

## See also

- [AGENTS.md](../AGENTS.md) — agent runbook and workflows
- [CHROMA_AND_STACK.md](./CHROMA_AND_STACK.md) — storage and locking
