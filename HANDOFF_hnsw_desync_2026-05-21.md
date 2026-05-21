# Handoff: ChromaDB SQLite ↔ HNSW desync fix

Date: 2026-05-21
Branch: `main` (2 commits ahead of origin)
Commits: `9514b7c`, `285acc2`

## Problem

User reported `tjs-pc` silo with 441 chunk IDs in SQLite but only 213 reachable
through HNSW (`Error executing plan: Internal error: Error finding id` in MCP).
Reported as recurring across multiple silos, not specific to `tjs-pc`.

## Root causes identified

1. **In-process singleton + writer client coexistence.** `op_repair_silo` and
   `mcp_server._run_reindex` both held a cached `get_client()` PersistentClient
   alive while invoking `run_add()`, which opened a second fresh PersistentClient
   via `writer_client()`. Two live `PersistentClient` instances on the same
   persist dir corrupt the HNSW segment writer (documented at
   [src/chroma_client.py:148](src/chroma_client.py#L148)).

2. **Cross-process reader holding stale Rust handle.** A long-lived reader
   (MCP server, watcher daemon) holds a `PersistentClient` open. A separate
   writer process (`llmli repair`, `pal pull`) mutates segments on disk. The
   reader's next query SIGSEGVs against the stale Rust mmap. ChromaDB 1.4+'s
   `clear_system_cache()` is itself unsafe (crashes), so the process must
   restart to recover.

3. **(Original 228/441 symptom was already resolved before this session
   started)** — `tjs-pc` had been rebuilt. Audit on the live DB now shows
   `truly_missing=0` across all silos (queue lag accounted for).

## What landed

### Commit `9514b7c` — in-process desync fix

| File | Change |
|---|---|
| [src/operations.py](src/operations.py) | `op_repair_silo` releases singleton before `run_add`; `op_remove_silo` and `op_repair_silo` bump generation after singleton delete; surfaces previously-swallowed delete errors via `record_index_error` |
| [src/operations.py](src/operations.py) | New `op_silo_hnsw_consistency(db_path)` — per-silo report. Wired into `op_chroma_diagnostics` (sqlite-only global summary; no client open) |
| [src/silo_audit.py](src/silo_audit.py) | New `verify_silo_hnsw_consistency(collection, slug, db_path)` using the pickle+sqlite invariant: `sqlite_ids − hnsw id_to_label − embeddings_queue` |
| [src/ingest/__init__.py](src/ingest/__init__.py) | `HnswWriteVerificationError` + `_verify_batch_write()` after each `collection.add()`, gated by `LLMLIBRARIAN_VERIFY_HNSW_WRITES` (default on); rebuild-path delete errors now logged + recorded |
| [mcp_server.py](mcp_server.py) | `_run_reindex` releases singleton before `run_add`; `health()` reports per-silo HNSW consistency |
| [tests/integration/test_hnsw_sqlite_consistency.py](tests/integration/test_hnsw_sqlite_consistency.py) | 3 tests: baseline, repair-silo, trigger-reindex pattern |
| [tests/unit/test_repair_singleton_release.py](tests/unit/test_repair_singleton_release.py) | 2 tests: call-order contract for `op_repair_silo` and `_run_reindex` |

### Commit `285acc2` — cross-process race fix

| File | Change |
|---|---|
| [src/chroma_client.py](src/chroma_client.py) | New `bump_generation(db_path)`, `check_for_writer_changes()`, `exit_if_stale()`. `writer_client` bumps generation on teardown. `get_client()` checks generation on cached-client return; if `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION=1`, calls `sys.exit(99)` so supervisor restarts |
| [mcp_server.py](mcp_server.py) | Sets `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION=1` at boot so MCP self-exits when a separate process bumps the generation |
| [tests/integration/test_concurrent_subprocess_hnsw.py](tests/integration/test_concurrent_subprocess_hnsw.py) | Reader script now opts in to exit-on-stale; t2 re-enabled and now asserts the reader exits with rc=99 on writer activity; final consistency check moved to a subprocess (pytest process's ChromaDB runtime gets tainted across tests once on-disk segments are mutated) |

## Test status

```
LLMLI_RACE_TEST=1 uv run pytest \
  tests/integration/test_concurrent_subprocess_hnsw.py \
  tests/integration/test_hnsw_sqlite_consistency.py \
  tests/unit/test_repair_singleton_release.py \
  tests/unit/test_chroma_client_release.py \
  tests/unit/test_chroma_lock.py
```

21 / 21 passing including t1 (concurrent adds), t2 (reader vs repair), t3
(watcher vs full reindex).

## How the new audit can be used

- **CLI**: `op_chroma_diagnostics` returns `hnsw_global_truly_missing` and
  `hnsw_global_queued` (sqlite-only, safe even if Chroma is unhealthy).
- **MCP**: `health()` returns `hnsw_consistency.desynced` with per-silo
  `{slug, sqlite_ids, missing_count, queued, missing_ids_sample}`.
- **Programmatic**: `from operations import op_silo_hnsw_consistency` then
  `op_silo_hnsw_consistency(db_path)`.

The invariant: `truly_missing = sqlite_owned − hnsw_id_to_label − embeddings_queue`.
A non-zero `truly_missing` is a real SQLite ↔ HNSW desync. `queued > 0` is
normal compaction lag and is NOT a desync.

## Chroma 1.x process-safety (upstream constraint)

ChromaDB is **thread-safe but not process-safe** for embedded `PersistentClient`
on one `persist_directory`. Chroma 1.x uses Rust HNSW + SQLite; two processes each
opening `PersistentClient` start separate Tokio/compaction runtimes on the same
files. Failure mode is often **SIGSEGV** (not a Python lock error) when HNSW
binary indices diverge from SQLite.

Official mitigation: run `chroma run` and use `HttpClient` from all clients.

### Follow-up landed (2026-05-21, post-handoff)

| Layer | Change |
|-------|--------|
| [src/chroma_client.py](src/chroma_client.py) | `preflight_embedded_write()` blocks CLI writes when MCP `/healthz` or active `pal pull --watch` detected; `LLMLIBRARIAN_CHROMA_HOST` enables `HttpClient` |
| [pal.py](pal.py) | `pal chroma install|start|stop|status|logs`; pull preflight before ingest |
| [mcp_server.py](mcp_server.py) | `health()` includes `chroma_transport`, `embedded_write_unsafe_with_cli`, `chroma_server_ok` |
| [scripts/run_chroma_server.sh](scripts/run_chroma_server.sh) | `chroma run --path $LLMLIBRARIAN_DB` |
| [deploy/systemd/llmlibrarian-chroma@.service](deploy/systemd/llmlibrarian-chroma@.service) | User unit template |

**Embedded mode:** `pal pull` while MCP is up → exit 1 with actionable message (not SIGSEGV).

**Server mode:** set `LLMLIBRARIAN_CHROMA_HOST=127.0.0.1`, `pal chroma start`, restart MCP — `pal pull` and MCP can run concurrently.

## Files for posterity

- `/tmp/audit_hnsw_desync.py` — standalone read-only audit script that uses
  the same pickle+sqlite invariant. Useful for ad-hoc DB inspection without
  loading the llmli code paths.

## Quick smoke commands

```bash
# Per-silo consistency report on the live DB (read-only, no Chroma client):
uv run python -c "
import sys; sys.path.insert(0, 'src')
from operations import op_chroma_diagnostics
d = op_chroma_diagnostics('my_brain_db')
print('truly_missing:', d.get('hnsw_global_truly_missing'),
      'queued:', d.get('hnsw_global_queued'))
"

# Full test run including race tests:
LLMLI_RACE_TEST=1 uv run pytest \
  tests/integration/test_concurrent_subprocess_hnsw.py \
  tests/integration/test_hnsw_sqlite_consistency.py \
  tests/unit/test_repair_singleton_release.py -v
```
