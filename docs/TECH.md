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
- For image-only pages, OCR fallback is attempted in deterministic order:
  1) PaddleOCR (if installed), then
  2) `tesseract` CLI (if available).
- OCR availability/fallback outcomes are logged as structured processor events.
- Missing OCR backends do not fail ingestion; indexing continues with warnings.

## Environment Variables (most used)

- `LLMLIBRARIAN_DB`
- `LLMLIBRARIAN_CONFIG`
- `LLMLIBRARIAN_MODEL`
- `LLMLIBRARIAN_TRACE`
- `LLMLIBRARIAN_RERANK`
- `PAL_DEBUG`

## Source of Truth Priority

1. `AGENTS.md`
2. Tests in `tests/unit/*`
3. This file (`docs/TECH.md`)
4. README usage examples
