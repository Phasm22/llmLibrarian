---
description: Implementation plan for the library activity ledger (PR 1) and the macOS Overview rebuilt on top of it (PR 2). Open when implementing or reviewing either PR.
type: plan
updated: 2026-10-18
---

# Library activity history + Overview

## Context

The Overview should tell the story of the library over **Today / 7 days / 30 days**, compared with the previous period. It covers three areas: Library changes, Library use and Attention needed. Above them sits a short factual summary.

None of today's sources can support that honestly:

- **Query audit** (`src/query_audit.py`): rotates by size (5 MB × 3), not by time. It has no explicit "no sources" flag, and the app reads only its last 100 lines.
- **Watcher `batch_drain` lines** (`pal.py:2102`): name at most 5 sample paths, don't tell an added file from an updated one, and cover only watcher-driven changes. `llmli add`, `pal pull`, MCP `update_file` and `remove_file`, and repair all leave no history. The app reads only the last 64 KB of each log.
- **`llmli_last_failures.json`**: holds at most 50 entries with no timestamp and no silo. Entries never clear when a file later succeeds.

Decisions you made:

- History lives in a **SQLite ledger**.
- The overview is readable from the **macOS app and `pal activity`**, with no MCP tool. Search text and private-silo activity therefore never reach a cloud model.
- The work ships as **two PRs**: record the events first, then build the Overview.

## PR 1 — Activity ledger (`src/activity_ledger.py`)

### Storage

- **File:** `<LLMLIBRARIAN_DB>/llmli_activity.sqlite3`. It sits next to the manifest and stays machine-local, like the index.
- **Connection settings:** WAL mode, `busy_timeout=5000`, one short transaction per write.
- **Writes never raise.** This copies `query_audit.record`: a ledger failure must never break ingest or retrieval.
- **Switches:**
  - `LLMLIBRARIAN_ACTIVITY=0` turns the ledger off. `tests/conftest.py` sets it to 0 by default, as it already does for the audit.
  - `LLMLIBRARIAN_ACTIVITY_RETENTION_DAYS` defaults to 180. The writer prunes old rows at most once a day, tracked in `meta`.

### Tables

| Table | Columns |
|---|---|
| `meta` | `key`, `value` (`schema_version`, `files_since`, `searches_since`, `last_prune`) |
| `file_events` | `ts`, `silo`, `path`, `kind` (`added`/`updated`/`removed`), `origin`, `size` |
| `silo_events` | `ts`, `silo`, `kind` (`silo_added`/`silo_removed`), `files` |
| `searches` | `ts`, `tool`, `silo` (null = unscoped), `query_text` (nullable), `unique_files`, `sources_found` (0/1), `client`, `cached`, `errored`, `truncated` |
| `search_hits` | `search_id`, `silo`, `path`: one row per **unique file** a search surfaced |
| `failures` | `silo`, `path`, `error_class`, `error`, `first_seen`, `last_seen`, `occurrences`, `resolved_at` |

- `failures` holds one row per open problem, not one row per occurrence, so repeated problems group themselves.
- Indexes go on `ts` for every event table and on `(silo, path)`.

### Where events are recorded

**1. File changes.** These are captured in `file_registry._update_file_manifest` (`src/file_registry.py:116`). Every writer already passes through it: `run_add` incremental and overwrite, `update_single_file`, `remove_single_file`, `state.py:403` and `cli.py:1117`.

- **Before the update:** snapshot the `{path: (hash, mtime, size)}` map for each silo.
- **After the write succeeds:** diff the two maps and record the events. This happens inside the existing lock.
  - A new path is `added`.
  - A missing path is `removed`.
  - A changed hash is `updated`. For zips the hash is `""`, so a change in mtime or size counts instead.
  - A new or vanished silo key produces a single `silo_events` row, not N file rows.
- **`origin`:** comes from a contextvar set by `activity_ledger.origin("watch"|"add"|"mcp"|"repair"|"rehydrate")`. The default is `"index"`.
  - Repair and rehydrate set `repair`/`rehydrate`, and the overview leaves those origins out of Library changes. A re-index is not a document change.
  - **Verify while implementing:** how `llmli repair` touches the manifest (clear-then-add vs overwrite). Make sure that path is wrapped.

**2. Failures.** These are captured in `state.set_last_failures` and `append_last_failures` (`src/state.py:421-451`).

- **Upsert:** a failure is matched on `(silo, path, error_class)`. A new one inserts a row; a known one bumps `last_seen` and `occurrences`.
- **Silo:** resolved by path prefix against the silo paths in the manifest. Implemented as `activity_ledger.silo_for_path` (longest root wins); `operations.py` keeps its own resolve-based check for `list_silos`.
- **`error_class`:** the exception type name, or a normalised leading phrase such as `unsupported`, `timeout`, `permission`.
- **Resolving:** an `added`/`updated` event for a path sets `resolved_at` on that path's open failures. This also fixes the "never clears" gap, though only in the ledger; the JSON file is unchanged.

**3. Searches.** These are captured in `query_audit.record()`, the single point every MCP query tool already calls.

- **Kept separate:** `read_document` records are not searches. They stay apart, which keeps "documents retrieved" distinct from "documents opened".
- **`unique_files` and `search_hits`:** built from the full `chunks` list, not the top-12 summary.
- **`sources_found`:** set when `unique_files > 0`. Wording throughout is "sources found", never "successful answers".
- **`client`:** taken from `query_audit.client_label`.
- **Opting out:** if `LLMLIBRARIAN_QUERY_AUDIT=0`, the search row is still written but `query_text` is null. Turning the audit off means "don't keep my query text".
- **CLI searches:** `llmli query` and `llmli multi-query` record through the same helper, with client `llmli`. `llmli ask` / `pal ask` are not recorded yet: `run_ask` returns text, not chunks, so their sources are unknown.

### Backfill

`pal activity backfill` imports the existing `query-audit.jsonl(.2,.1,live)` files into `searches` and `search_hits`, using `read_records`.

- It is idempotent: a meta flag marks it done, and it dedupes on `ts + tool + query`.
- It sets `searches_since` to the earliest imported `ts`.
- File history cannot be rebuilt from 5-path samples, so `files_since` stays the ledger's creation time.

### Read side: `overview(db_path, window, now=None, silo=None)`

**Windows.** Bucket sizes are local time.

| Window | Period | Previous period | Buckets |
|---|---|---|---|
| `today` | Local midnight to now | Yesterday midnight to the same time of day | Hourly |
| `7d` | Rolling | The prior 7 days | Daily |
| `30d` | Rolling | The prior 30 days | Daily |

**What it returns:**

- **`history`:** `{files_since, searches_since, full_comparison}`. When `full_comparison` is false, callers show "History available since …" and drop the comparison sentence.
- **`changes`:**
  - totals for added/updated/removed, current vs previous
  - `by_silo`, ranked
  - `timeline`
  - files per silo, for drill-down
- **`use`:**
  - search count, current vs previous
  - searches with sources and without
  - `by_silo`: searches and unique files
  - `top_documents`: by the number of searches that surfaced them, not by chunk count
  - `by_client`
  - `timeline`
- **`attention`:**
  - open failures grouped by `(silo, error_class)`, with a count, sample paths and `last_seen`
  - searches with no sources grouped by normalised query text, with a count, silo and last time seen
  - silos with unresolved failures
- **`health`:** every registered silo, including ones nobody searched. Each row carries file count, last change, last search, private flag and open failures. With `check_staleness=True` it also adds `stale_file_count` from `operations._inject_staleness`. That is opt-in because it walks folders.
- **`summary`:** a deterministic headline plus sentences built from templates.
  - "**Project Notes was the busiest collection this week.**"
  - "Most document changes were in Research and Project Notes."
  - "Searches rose to 42 from 30 the previous week; 3 found no sources."
  - It never infers answer quality.

### CLI

```
pal activity [--window today|7d|30d] [--silo S] [--check-staleness] [--json]
pal activity backfill
```

- The text output is the summary followed by the three areas.
- `--json` returns the `overview()` dict. This is also the contract the Swift side mirrors.
- `pal uninstall --purge-data` must remove the SQLite file. Check whether it is already covered by purging the DB directory.

### Docs

- **`docs/TECH.md`:** add an "Activity ledger" section covering the schema, emit points, retention and privacy.
- **`CLAUDE.md`:** add a short paragraph next to "Query audit".
- **`AGENTS.md`:** add a CLI line.
- **New `docs/plans/library-activity-overview.md`:** a copy of this plan, with frontmatter.

### Tests: new `tests/unit/test_activity_ledger.py`

An autouse fixture turns the ledger on, using `tmp_path`.

- **Manifest diff:** add, update and remove each produce events. Same hash produces none. A zip mtime change counts as an update. Silo removal produces one `silo_events` row. A write with `repair` origin is left out of the overview's changes.
- **Failures:** upsert and grouping, resolution on a later successful update, and silo matching by path prefix.
- **Searches:** unique-file counting across chunks, the `search_hits` rows, `sources_found=0` for empty results, null text when the audit is off, and `read_document` left out.
- **Overview math** with an injected `now`: today vs yesterday-to-date, the 7d and 30d previous periods, `full_comparison=false` when `searches_since` falls inside the previous period, and health listing unsearched silos.
- **Housekeeping:** backfill is idempotent, and retention prunes old rows.
- **Concurrency:** two threads writing at once lose nothing. Use threads only, not multiprocessing from stdin (see memory).
- **Existing tests:** extend `test_query_audit.py` so `record()` also writes a ledger row.
- **Shared fixture (moved to PR 2):** a fixture generator writes `tests/fixtures/activity/ledger.sqlite3` plus `overview-7d.json` at a fixed `now`, and the Swift test checks against the same pair.

## PR 2 — macOS Overview

### Data layer

- **`Services/ActivityLedger.swift`** uses Foundation and `import SQLite3`, and opens the database read-only. Add `linkerSettings: [.linkedLibrary("sqlite3")]` to `Package.swift`. The SQL mirrors the Python `overview()` definitions, and the shared fixture keeps the two in step.
- **Models:** `Model/Models.swift` gains `ActivityWindow` (`today`, `week`, `month`) and an `ActivityOverview` struct that mirrors the JSON contract.
- **Store:** `Collector.collect` reads the overview for the selected window. The store gets `@Published activity` and `activityWindow`, the latter saved with `@AppStorage`. It runs on the existing 12s refresh; the queries hit `ts` indexes.

### `Views/OverviewView.swift`, rebuilt top to bottom

1. **Header:** the existing status line, plus a segmented **Today / 7 days / 30 days** picker.
2. **Summary:** the headline and sentences, plus a "History available since …" footnote when the comparison period isn't fully covered.
3. **Timeline:** two small Swift Charts bar charts, "Document changes" (stacked added/updated/removed) and "Searches". They stay separate on purpose.
4. **Library changes:** ranked silo rows showing +added, ~updated, −removed and the change vs the previous period. A disclosure reveals the files.
5. **Library use:** the search count with its delta, sources found vs none, ranked collections, top documents and assistants. A disclosure reveals the queries, and a row jumps to the Activity page.
6. **Attention needed:** failure groups, no-source search groups, and silos falling behind. "Falling behind" combines the open-failure count from the ledger with the existing watcher-stopped and missing-folder issues from `store.issues`. Each group expands to its files or queries.
7. **Collection health:** every silo with files, last change, last search, watcher state and the private lock.

The "Chunks" `StatTile` goes away; chunk counts stay on the Silos page. Services and Connect move below the fold or rely on their own pages.

**Supporting changes:**

- `SampleLibrary.swift` gains sample activity, so snapshot and screenshot mode still render.
- A new `macos/Tests/ActivityLedgerTests/main.swift`, registered in `test.sh`, reads the shared fixture DB and checks the totals against `overview-7d.json`.

## Verification

- **PR 1:**
  - `uv run pytest tests/unit -q` passes.
  - Manual check, in order:
    1. Run `pal activity backfill`.
    2. Run `pal activity --window 7d` and confirm the search counts roughly match `pal queries --since 7d --summary`.
    3. Touch, add and delete a file in a watched silo, then confirm the matching `file_events` appear in `pal activity --window today --json`.
    4. Run an MCP query that finds nothing, then confirm it appears under `attention.no_source_searches`.
    5. Run `llmli repair <silo> --dry-run`, then a real repair on a small silo, and confirm no change spike.
- **PR 2:**
  1. `macos/test.sh` passes.
  2. `macos/build.sh` succeeds.
  3. Run the app against the live ledger and click through all three windows and the drill-downs.
  4. Capture screenshots in snapshot mode (`LLMLIBRARIAN_SNAPSHOT_DIR`) and compare them with the sample library.
