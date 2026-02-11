# Technical Reference (Operator Guide)

This is the operator reference for llmLibrarian.  
Use the main [README](../README.md) for onboarding and quick start.

## Query Decision Pipeline

High-level ask flow:
1. Intent routing  
- Query classified into intent (lookup, aggregate, profile, capabilities, deterministic file list, etc.)
2. Deterministic guardrails  
- If a deterministic path applies (for example rank/file-year/value guardrails), it short-circuits generation.
3. Retrieval + ordering  
- Scope, retrieval, rerank/diversify, and source-priority logic applied before model call.
4. LLM fallback  
- Only when deterministic paths are not sufficient.
5. Footer/trace  
- Source footer built from retrieved evidence; trace optionally written via env.

## Confidence + Scope Signals

Low-confidence meaning:
- Retrieval distances indicate weak relation to indexed content, or mixed evidence quality.

Scope behavior:
- Explicit `--in <silo>` always wins.
- Natural-language scope phrases are conservative best-effort auto-binding.
- Ambiguous matches do not auto-bind.

Weak-scope retry:
- For unscoped asks with weak initial matches, the system may do a lightweight catalog-based single-silo retry.
- Retry is deterministic and bounded (top candidate only).

Catalog diagnostics:
- `--explain` prints deterministic catalog/scope diagnostics to stderr when applicable.
- `--force` allows deterministic catalog queries to run even when scope is stale.

## Source Footers

Footer behavior:
- Sources are de-duplicated by source path.
- Line/page markers from multiple chunks are aggregated per source.
- Score shown is based on best-ranked chunk per source.

Example style:
- `file.pptx (line 1, 23, 51) · 0.54`

## Layout

- `pyproject.toml` — dependencies and packaging.
- `cli.py` — `llmli` argparse CLI.
- `pal.py` — `pal` Typer CLI orchestration layer.
- `archetypes.yaml` — prompts, limits, query options.
- `src/ingest.py` — indexing and incremental update pipeline.
- `src/query/core.py` — query orchestration entry point.
- `src/query/*.py` — intent, retrieval, guardrails, formatting, scope binding, trace.
- `src/llmli_evals/adversarial.py` — synthetic trustfulness evaluation harness.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `LLMLIBRARIAN_DB` | DB path (default `./my_brain_db`) |
| `LLMLIBRARIAN_CONFIG` | Config path (default `archetypes.yaml`) |
| `LLMLIBRARIAN_MODEL` | Ollama model (default `llama3.1:8b`) |
| `LLMLIBRARIAN_TRACE` | Append per-ask JSON lines (debug/audit) |
| `LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE` | Relevance threshold override |
| `LLMLIBRARIAN_RERANK=1` | Enable reranker (if installed) |
| `LLMLIBRARIAN_EDITOR_SCHEME` | Source link scheme: `vscode`, `cursor`, `file` |
| `PAL_DEBUG=1` | Emit debug scoping diagnostics to stderr |
| `LLMLIBRARIAN_CHUNK_SIZE` | Chunk size override |
| `LLMLIBRARIAN_CHUNK_OVERLAP` | Chunk overlap override |
| `LLMLIBRARIAN_MAX_WORKERS` | File processing worker cap |
| `LLMLIBRARIAN_ADD_BATCH_SIZE` | Embedding batch size |
| `LLMLIBRARIAN_EMBEDDING_WORKERS` | Embedding worker count |
| `LLMLIBRARIAN_DEDUP_CHUNK_HASH=1` | Enable retrieval dedup by chunk hash |

## Operational Notes

Interrupting add/pull:
- Safe for source files (read-only input).
- Partial index states can occur if interrupted mid-run; rerun `pal pull <path>` or `llmli add <path>` for consistency.

Self-silo (dev mode):
- `pal ask`/`pal capabilities` may warn when self-silo is stale.
- Refresh with `pal sync`.

## Eval Commands

Adversarial trust eval:

```bash
uv run llmli eval-adversarial --out ./adversarial_eval_report.json
uv run llmli eval-adversarial --limit 20 --no-strict-mode --direct-decisive-mode --out ./adversarial_eval_smoke.json
```
