# Technical Reference

Current runtime contracts only.

## Query Contract

`run_ask` follows this order:
1. Intent routing
2. Deterministic branches and guardrails
3. Retrieval and ordering
4. LLM answer fallback
5. Source footer and optional trace write

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

Watch lifecycle:
- start: `pal pull <path> --watch`
- status: `pal pull --status`
- stop: `pal pull --stop <target>`

Watcher locks live in `~/.pal/watch_locks/*.pid`.

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

Adaptive image behavior:
- OCR happens at ingest
- low-signal OCR gibberish is dropped
- multimodal image vision is off by default
- when `image_vision_enabled` is true for a silo, text-heavy or structured images may be summarized eagerly
- when `image_vision_enabled` is true for a silo, obvious natural-photo images are deferred
- query may lazily summarize at most one deferred image hit, then cache it back to the artifact as `cached_query_time`
- silos with `image_vision_enabled=false` never run multimodal image vision at ask time

Requirements for standalone images:
- `LLMLIBRARIAN_VISION_MODEL` must be a vision-capable Ollama model when `image_vision_enabled` is true
- OpenCLIP image embedding dependencies must be installed
- if image embeddings are unavailable, standalone image ingest fails fast
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
- `PAL_DEBUG`

## Source Priority

1. `AGENTS.md`
2. tests
3. this file
4. README examples
