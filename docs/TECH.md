# Technical Reference

Current runtime contracts only. Narrative guide: [GUIDE.md](./GUIDE.md).

## Product Shape

The system is organized around a few stable ideas:
- `pal` is the operator-facing wrapper for ingest, ask, and health checks.
- `llmli` is the direct engine CLI for lower-level actions and automation.
- most behavior is deterministic and driven by the index, not by ad hoc prompts.

## Query Contract

`run_ask` follows this order:
1. Intent routing
2. Deterministic branches and guardrails
3. Retrieval and ordering
4. LLM answer fallback
5. Source footer and optional trace write

Scoped retrieval can run dual streams (`<silo>` + `<silo>-artifacts`) with
per-stream source diversity and RRF merge when artifact metadata exists.

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
- `llmli repair` (L1) for per-silo wipe + full re-index
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

## Common Environment Variables

- `LLMLIBRARIAN_DB`
- `LLMLIBRARIAN_CONFIG`
- `LLMLIBRARIAN_MODEL`
- `LLMLIBRARIAN_VISION_MODEL`
- `LLMLIBRARIAN_TRACE`
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
