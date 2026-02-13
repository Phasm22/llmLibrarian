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
