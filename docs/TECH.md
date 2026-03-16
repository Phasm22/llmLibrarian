# Technical Reference (Current)

Operator-focused runtime contracts only.

## Core Query Contract

`run_ask` follows this order:
1. Intent routing
2. Deterministic branches/guardrails (when applicable)
3. Retrieval + ordering (when deterministic branch does not apply)
4. LLM answer fallback
5. Source footer + optional trace write

## Deterministic Paths

Current deterministic query families:
- Capabilities (supported file types)
- Code language stats
- Project count
- File-list by year
- Structure snapshots (`outline`, `recent`, `inventory`, extension counts)
- Tax ledger resolver (W-2/1040/1099 key fields; employer/year-scoped lookups)
- Direct value guardrails (when extraction is confident)

For deterministic structure asks:
- Metadata source is manifest/registry, not embedding top-k.
- Unscoped quiet mode returns a single-line policy error.

## Scope Rules

- Explicit `--in <silo>` always wins.
- `pal ask in <silo> "..."` is normalized to explicit `--in` by `pal`.
- Ambiguous scope binding does not auto-apply.

## Confidence and Warnings

- Confidence warnings are heuristic and intent-aware.
- Self-silo stale warning is a dev-mode freshness warning, not a hard error.

## Watch Lifecycle (`pal pull`)

- Start: `pal pull <path> --watch`
- Status: `pal pull --status`
- Stop: `pal pull --stop <target>`

Lock files:
- `~/.pal/watch_locks/*.pid` (JSON metadata)

## Tracing

If `LLMLIBRARIAN_TRACE` is set, asks append JSON-lines traces.
Trace includes `answer_kind` (`catalog_artifact`, `guardrail`, or `rag`).

## Tax Ledger Architecture

- Ingest writes normalized tax rows with provenance:
  - `source`, `page`, `tax_year`, `form_type`, `field_code`, `normalized_decimal`, `extractor_tier`, `confidence`.
- Extraction is tiered:
  - Tier A: direct form-field labels (`form_field`)
  - Tier B: layout label/value matching (`layout`)
  - Tier C: OCR text + layout matching (`ocr_layout`)
- Tax query handling is terminal and deterministic:
  - resolved value with cited sources, or
  - abstain/disambiguation with reason category.
- LLM does not generate numeric tax values.
- Tax QA Hardening v2 includes an in-place real-life W-2 phrasing harness in unit and integration tests.

## Tax QA TODOs

- Normalize weak/no-data behavior for year-scoped total-income asks so outputs are consistent across years.
- Prevent cross-year leakage in tax-year filters (for example, 2022 asks pulling 2021 return pages).
- Tighten 1040 line extraction to reject line-number artifacts (for example, `1.00` on line 9).
- Disambiguate multi-page same-form conflicts (same tax year + same form but duplicate/worksheet variants).
- Keep tax response sources grounded and clickable (absolute source paths), and suppress empty source sections.
- Ensure tax-year derivation prefers filename year over parent-folder year (`/Tax/2022/2021_TaxReturn.pdf` should map to 2021).
- For total-income asks, avoid using worksheet-only pages (for example, Form 8615 worksheet pages) as line-9 evidence.
- Add diagnostics that list candidate extracted values during tax conflicts so users can see why abstain happened.

## OCR Observability

- PDF extraction uses PyMuPDF text first.
- Standalone image files (`.png`, `.jpg`, `.jpeg`, `.heic`, `.heif`, `.tif`, `.tiff`) are first-class indexed inputs and now index as:
  1) one `image_summary` chunk, plus
  2) one or more `image_region` chunks when OCR finds meaningful text zones.
- Standalone image files also write one image-vector row into a sibling collection (`llmli_image` for unified ask, `<collection>_image` for archetype collections). The join key is `parent_image_id`.
- Image chunks use scalar-only Chroma metadata such as `record_type`, `source_modality=image`, `parent_image_id`, `region_index`, `region_role`, bbox fields, and `image_artifact_relpath`.
- Image-vector rows keep retrieval-friendly scalar metadata (`record_type=image_vector`, `parent_image_id`, `source`, `silo`, `image_embedding_backend`) and store the visual embedding separately from the text collection.
- Raw Vision observations and image-analysis artifacts are written under `<db>/image_artifacts/<file_hash>.json`.
- In auto mode on macOS, OCR is attempted in deterministic order:
  1) Vision (via a cached Swift helper), then
  2) PaddleOCR (if installed), then
  3) `tesseract` CLI (if available).
- In auto mode off macOS, OCR is attempted in deterministic order:
  1) PaddleOCR (if installed), then
  2) `tesseract` CLI (if available).
- `LLMLIBRARIAN_OCR_BACKEND` can pin OCR to `vision`, `paddleocr`, or `tesseract`; pinned mode disables fallback.
- OCR fallback text is quality-gated; low-signal gibberish from fallback OCR is dropped instead of indexed.
- Standalone image enrichment requires `LLMLIBRARIAN_VISION_MODEL` to point at a vision-capable Ollama model. If standalone images are present and the model is missing or text-only, `pal pull` / `llmli add` now fail fast.
- Standalone image embeddings use the local OpenCLIP backend. If the OpenCLIP dependencies are missing, standalone image ingest now fails fast instead of silently skipping the image-vector collection.
- Query retrieves from both the text collection and the image-vector collection, then materializes the winning image hits back into `image_summary` / `image_region` text evidence before synthesis.
- OCR availability/fallback outcomes are logged as structured processor events.
- Missing OCR backends do not fail ingestion; indexing continues with warnings.

## Environment Variables (most used)

- `LLMLIBRARIAN_DB`
- `LLMLIBRARIAN_CONFIG`
- `LLMLIBRARIAN_MODEL`
- `LLMLIBRARIAN_VISION_MODEL`
- `LLMLIBRARIAN_TRACE`
- `LLMLIBRARIAN_RERANK`
- `LLMLIBRARIAN_OCR_BACKEND`
- `PAL_DEBUG`

## Source of Truth Priority

1. `AGENTS.md`
2. Tests in `tests/unit/*`
3. This file (`docs/TECH.md`)
4. README usage examples
