---
description: Open when you need the current runtime contract for query, retrieval, ingest, or MCP behavior, rather than the narrative guide.
type: reference
globs:
  - "src/**/*.py"
  - "mcp_server.py"
updated: 2026-10-18
---

# Technical Reference

Current runtime contracts only. Narrative guide: [GUIDE.md](./GUIDE.md).

## Product Shape

The system is organized around a few stable ideas:
- `pal` is the operator-facing wrapper for ingest, ask, and health checks.
- `llmli` is the direct engine CLI for lower-level actions and automation.
- most behavior is deterministic and driven by the index, not by ad hoc prompts.

## Markdown and frontmatter

- `.md` / `.markdown` / `.mdx` files parse leading YAML frontmatter into Chroma
  chunk metadata (scalars only; lists become comma-delimited strings). The YAML
  block is stripped from the embedded text and replaced by a short one-line
  preamble. Malformed frontmatter is ignored — ingest continues on the raw body.
- Markdown at or under `MD_SINGLE_CHUNK_MAX` (default 4000 chars, override with
  `LLMLIBRARIAN_MD_SINGLE_CHUNK_MAX`) is one chunk. Larger files split on ATX
  headings, then fall back to size-based chunking. Other file types keep the
  existing `CHUNK_SIZE` / `CHUNK_OVERLAP` path.
- MCP `query_personal_knowledge` / `multi_query_knowledge` accept metadata
  filters (`category`, `main_protein`, `min_servings`, `max_servings`,
  `max_total_time`, `tags`, `course`, `era`) and return a per-chunk `metadata`
  object when frontmatter was present. **Reindex the silo** after upgrading so
  existing chunks pick up frontmatter metadata
  (`pal pull --full <folder>` or `repair_silo` / `llmli repair <silo> --yes`).

## Query Contract

`run_ask` follows this order:
1. Intent routing
2. Deterministic branches and guardrails
3. Retrieval and ordering
4. LLM answer fallback
5. Source footer and optional trace write

Scoped retrieval can run dual streams (`<silo>` + `<silo>-artifacts`) with
per-stream source diversity and RRF merge when artifact metadata exists.

Hybrid retrieval (`query/retrieval.py`, `run_hybrid_retrieve`) fuses the vector
list with a lexical list by RRF, which reads list position as relevance. The
lexical list is therefore BM25-ordered, never storage-ordered:
- terms: quoted phrases, numbers, and 3+ letter words minus
  `DIRECT_LEXICAL_STOPWORDS`, lowercased, at most 8
- matching: case-insensitive Chroma `$regex` with a leading word boundary
  (`lexical_term_pattern`)
- a term in more than `LEXICAL_MAX_DF_RATIO` (20%) of the scoped chunks is
  dropped once the scope has at least 50 chunks; no surviving term means
  `vector_only`
- the 200-hit cap (`MAX_LEXICAL_FOR_RRF`) applies after BM25 sorting
- a failed lexical lookup logs a warning and falls back to `vector_only`

`explain_retrieval` reports the method actually taken and a `lexical_terms`
block (terms used, terms dropped and why). Background:
[RCA_HYBRID_LEXICAL_RANKING_RECIPES_SILO.md](./RCA_HYBRID_LEXICAL_RANKING_RECIPES_SILO.md).

Deterministic query families:
- capabilities
- code language stats
- project count
- file-list by year
- structure snapshots
- tax ledger resolver
- direct value guardrails

Scope rules:
- explicit `--in <silo>` wins
- `pal ask in <silo> "..."` is normalized to `--in`
- ambiguous scope phrases do not auto-bind

## Pull Contract

`pal pull <path>` / `llmli add <path>`:
- index supported files
- reuse existing chunks when possible
- refuse cloud-sync roots by default
- print preflight counts for supported and skipped major file types
- print image progress for image-heavy pulls
- accept per-run `--workers` and `--embedding-workers`
- persist per-silo `--image-vision`

File state:
- `llmli_file_manifest.json` is the single source of truth for per-silo indexed files
- content-hash lookup and silo path catalogs are derived from it in memory, cached on the manifest's `(mtime, size)` so another process's write is picked up on the next read
- `llmli_file_registry.json` is retired: never read, and deleted on the next manifest write
- a `pal`/MCP process started before this change keeps recreating that file until it is restarted — check the running process, not the code on disk

Watch lifecycle:
- start: `pal pull <path> --watch`
- status: `pal pull --status`
- stop: `pal pull --stop <target>`

Watcher locks live in `~/.pal/watch_locks/*.pid`.

The broader maintenance and inspection surfaces are intentionally thin wrappers around the same index state: use them when you need to confirm freshness, support, or cleanup, not as separate product modes.

Repair/recovery surfaces:
- `llmli repair <silo> --yes` (L1) for per-silo wipe + full re-index (`--dry-run` to preview)
- `llmli repair-ladder` (L2 diagnostics) for sqlite integrity + segment scan
- `llmli rehydrate` (L3 helper) for registry-driven rebuild

## OCR and Images

PDFs:
- use PyMuPDF text first
- fall back to OCR when needed

OCR fallback order:
- macOS auto mode: Vision, then PaddleOCR, then `tesseract`
- non-macOS auto mode: PaddleOCR, then `tesseract`
- `LLMLIBRARIAN_OCR_BACKEND` can pin `vision`, `paddleocr`, or `tesseract`

Standalone images:
- supported: `.png`, `.jpg`, `.jpeg`, `.heic`, `.heif`, `.tif`, `.tiff`
- index as one `image_summary` chunk plus `image_region` chunks when OCR finds meaningful text
- also write one image-vector row into a sibling image collection
- store raw Vision/output artifacts under `<db>/image_artifacts/<file_hash>.json`
- keep Chroma metadata scalar-only
- extract embedded photo metadata when available: capture time, camera/lens, dimensions,
  orientation, exposure settings, and GPS coordinates; retrieval groups these scalar fields
  under `photo_metadata`

Reverse geocoding (`src/geocode.py`, off by default):
- `LLMLIBRARIAN_REVERSE_GEOCODE=1` resolves a photo's GPS pair against OpenStreetMap
  Nominatim at ingest and adds `place_name`, `place_address`, `place_category` and
  `nearby_places`, plus `Location:` / `Nearby:` lines in the summary chunk text
- off by default because it is the only part of ingestion that leaves the machine, and
  photo coordinates are usually a home, a workplace, or a school
- indoor GPS drifts 10-30 m, so the containing feature is often a parking lot; a bounded
  search of `LLMLIBRARIAN_NEARBY_CATEGORIES` (default `restaurant,cafe,bar,hotel`) supplies
  named venues, and the nearest within 50 m becomes `place_name`. Set the variable empty to
  skip that pass
- results cache forever in `~/.pal/geocode-cache.json` (override with
  `LLMLIBRARIAN_GEOCODE_CACHE`), keyed at ~1 m, which is what keeps Nominatim's 1 req/sec
  policy affordable across re-ingests
- every failure path returns no location rather than raising: a disabled, offline or slow
  geocoder must never fail an ingest run

Adaptive image behavior:
- OCR happens at ingest
- low-signal OCR gibberish is dropped
- multimodal image vision is off by default
- when `image_vision_enabled` is true for a silo, text-heavy or structured images may be summarized eagerly
- when `image_vision_enabled` is true for a silo, obvious natural-photo images are deferred
- query may lazily summarize at most one deferred image hit, then cache it back to the artifact as `cached_query_time`
- silos with `image_vision_enabled=false` never run multimodal image vision at ask time
- MCP `query_personal_knowledge` routes explicit photo/image queries to image-vector
  results, merges image candidates for implicit mixed queries, and returns
  `image_search` diagnostics; deferred candidates include a
  deterministic `ask_image` recommendation instead of allowing text-only misses
  to be presented as evidence that no photo exists

Requirements for standalone images:
- install the image embedding dependencies with `uv sync --extra image`
- the image extra includes `pillow-heif`; HEIC/HEIF is not decodable by base Pillow alone
- `LLMLIBRARIAN_VISION_MODEL` must be a vision-capable Ollama model when `image_vision_enabled` is true
- OpenCLIP image embedding dependencies must be installed
- OpenCLIP runs on CPU by default: on Apple Silicon, MPS pinned ~1.6 GB of GPU memory in the
  resident MCP server with no measurable per-image speedup. Set
  `LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE=mps` (or `cuda`) to override
- text and image embedding backends are initialized before extraction or Chroma
  mutation; if either is unavailable, standalone image ingest fails fast
- if image vision is enabled and the model is missing/non-vision, ingest fails fast

## Tax Contract

- ingest writes normalized tax rows with provenance
- tax answers are deterministic and resolver-backed
- the LLM does not generate numeric tax values
- output is either a grounded answer or an abstain/disambiguation reason

## Tracing

If `LLMLIBRARIAN_TRACE` is set, asks append JSON-lines traces.

## Activity Ledger

`src/activity_ledger.py` keeps a durable, time-ranged history in
`<LLMLIBRARIAN_DB>/llmli_activity.sqlite3` (WAL, machine-local like the index).
Two reads, used by both `pal activity` and the macOS app:

- `summary(window)` — totals with previous-period deltas, sparkline buckets, one row
  per registered collection, open failure groups. O(collections + buckets).
  `pal activity [--window today|7d|30d] [--json]`.
- `detail(kind, window, silo=, no_source_only=)` — the rows behind one number
  (`changes`, `searches`, `documents`, `clients`, `failures`), paged.
  `pal activity --detail searches --no-sources`.

Event semantics live once, in SQL views the ledger creates (`activity_changes_v1`,
`activity_searches_v1`, `activity_search_silos_v1`, `activity_failure_groups_v1`,
`activity_collection_events_v1`): re-index origins excluded, local-time day/hour
buckets, a search touches its scoped silo plus every silo it surfaced a file from.
Readers only filter by `ts` and group; `SUMMARY_SQL` and `ActivityLedger.swift`
carry identical query strings, and `tests/fixtures/activity` pins both readers to
the same output. A ledger written before the views existed gains them the next
time any current Python process opens it.

| Table | Written from | Notes |
|---|---|---|
| `file_events` | `file_registry._update_file_manifest` (diff before/after) | `added` / `updated` (hash change; mtime/size for zips) / `removed`; `origin` from `activity_ledger.origin()` |
| `silo_events` | same | A silo key appearing or vanishing is one event, not N file events |
| `failures` | `state.set_last_failures` / `append_last_failures` | One open row per (silo, path, error class); repeats bump `occurrences`; a later successful index sets `resolved_at` |
| `searches` + `search_hits` | `mcp_server._emit_query_audit`, `llmli query` / `multi-query` | One row per request, one hit per **unique file**; `read_document` is not a search |

Rules the overview relies on:

- `repair` / `rehydrate` origins re-index existing files; they are excluded from Library changes.
- "Sources found" means at least one file came back. It says nothing about answer quality.
- `meta.files_since` / `meta.searches_since` mark where history begins; a comparison is
  only reported when that precedes the previous period. `pal activity backfill` imports
  past searches from `query-audit.jsonl` (file history cannot be reconstructed).
- Private silos keep counts but lose paths and query text unless the caller names the
  silo or passes `--include-private`.
- `LLMLIBRARIAN_QUERY_AUDIT=0` still counts searches but stores no query text.
- Writes never raise; `LLMLIBRARIAN_ACTIVITY=0` disables the ledger. Events older than
  `LLMLIBRARIAN_ACTIVITY_RETENTION_DAYS` (default 180) are pruned once a day.

## Common Environment Variables

- `LLMLIBRARIAN_DB`
- `LLMLIBRARIAN_CONFIG`
- `LLMLIBRARIAN_MODEL`
- `LLMLIBRARIAN_VISION_MODEL`
- `LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE`
- `LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS` (MCP server drops cached embedding/rerank models after this long
  without use; default 900, `0` keeps them loaded)
- `LLMLIBRARIAN_TRACE`
- `LLMLIBRARIAN_ACTIVITY` / `LLMLIBRARIAN_ACTIVITY_RETENTION_DAYS` (activity ledger on/off; retention, default 180 days)
- `LLMLIBRARIAN_RERANK`
- `LLMLIBRARIAN_OCR_BACKEND`
- `LLMLIBRARIAN_ARTIFACT_SILOS`
- `LLMLIBRARIAN_ARTIFACT_MAX_FACTS`
- `LLMLIBRARIAN_ARTIFACT_MAX_INPUT_CHARS`
- `LLMLIBRARIAN_CONTEXT_BUDGET_TOKENS`
- `PAL_DEBUG`

## Source Priority

1. `AGENTS.md`
2. tests
3. this file
4. README examples
