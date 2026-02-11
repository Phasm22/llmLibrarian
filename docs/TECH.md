# Technical reference

Project layout, environment, tuning, and operational details. For philosophy and quick start, see the main [README](../README.md).

## Layout

- **pyproject.toml** — Project + deps; **uv** for venv and install (`uv venv`, `uv sync`).
- **cli.py** — llmli entrypoint: `llmli add|ask|ls|index|rm|log|capabilities|inspect`
- **pal.py** — Agent CLI (Typer): Do = `pal pull|ask`, See = `pal ls|inspect|capabilities|log`, Tune = `pal ensure-self|silos|tool`; state in `~/.pal/registry.json`.
- **archetypes.yaml** — Prompts (and optionally index config). **Prompt-only:** add an entry with `name` + `prompt` and an id that matches a silo slug (e.g. `stuff`); then `pal ask --in stuff "..."` uses that prompt. **Full:** include `folders`, `include`/`exclude`, `collection` for `llmli index --archetype X` and a separate collection. Limits (max_file_size_mb, etc.) apply to add/index.
- **src/** — Librarian code:
  - **embeddings.py**, **load_config.py**, **style.py**, **reranker.py**, **state.py**, **floor.py** — Support modules.
- **ingest.py** — Indexing core (archetype + add); **query_engine.py** — Query (ask).
- **ingest.py (single-file)** — `update_single_file()` / `remove_single_file()` support event-driven updates from `pal pull <path> --watch`.
  - **indexer.py** / **query.py** — Thin wrappers that re-export from ingest / query_engine for the CLI.
- **docs/CHROMA_AND_STACK.md** — Chroma usage and stack choices.
- **gemini_summary.md** — Project manifest and recovery notes.

Internal flags (dev-only): `llmli add --silo <slug> --display-name <name>` are used by `pal` to force the dev self-silo. Not intended for normal use.

## Development setup

This repo uses **uv** for venv and install:

- `uv venv` — create `.venv`
- `uv sync` — install from `pyproject.toml` and lock
- `uv run llmli ls` — run without activating the venv

Alternatives: **rye** (init, sync, run), **pip + venv** (slower, no lockfile), **Poetry** (heavier).

## Environment

| Variable | Purpose |
|----------|---------|
| `LLMLIBRARIAN_DB` | DB path (default: `./my_brain_db`) |
| `LLMLIBRARIAN_CONFIG` | Path to `archetypes.yaml` |
| `LLMLIBRARIAN_MODEL` | Ollama model (default: `llama3.1:8b`) |
| `LLMLIBRARIAN_LOG=1` | Enable indexing log to `llmlibrarian_index.log` |
| `LLMLIBRARIAN_RERANK=1` | Enable reranker (requires `sentence-transformers`) |
| `LLMLIBRARIAN_EDITOR_SCHEME` | Source links: `vscode` (default), `cursor`, or `file` |
| `LLMLIBRARIAN_TRACE` | If set to a file path, each `ask` appends one JSON line (intent, n_stage1, n_results, model, silo, num_docs, time_ms, query_len, hybrid, and a retrieval receipt: source paths and chunk hashes for the chunks sent to the LLM) for debugging and audit. |
| `PAL_DEBUG=1` | When catalog sub-scope routing triggers, print `scoped_to=N paths token=...` to stderr |
| `LLMLIBRARIAN_CHUNK_SIZE` | Chunk size in chars (100–4000; default 1000) |
| `LLMLIBRARIAN_CHUNK_OVERLAP` | Overlap (0–half of size; default 100) |
| `LLMLIBRARIAN_MAX_WORKERS` | File-level parallelism cap |
| `LLMLIBRARIAN_ADD_BATCH_SIZE` | Chunks per embedding batch (1–2000; default 256) |
| `LLMLIBRARIAN_EMBEDDING_WORKERS` | Parallel embedding workers (default 1; >1 enables async embedding compute) |
| `LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE` | Max Chroma distance for relevance gate |
| `LLMLIBRARIAN_DEDUP_CHUNK_HASH=1` | Enable in-retrieval dedup by chunk hash |

Limits (max_file_size_mb, max_depth, max_archive_size_mb, max_files_per_zip) are in `archetypes.yaml` or config.

## Chunking and scaling

Defaults suit a mix of code and docs. Chunking is line-aware (1000 chars, 100 overlap). Embedding runs in the main thread in batches; file read + chunking use a thread pool. The bottleneck is usually Chroma’s embedding. Use the env vars above to scale up (bigger batches, more workers) or down (smaller chunks for code-heavy silos).

## Interrupting ingestion (Ctrl+C)

Ingestion only **reads** your files and **writes** to the Chroma DB and (at the end) the silo registry. If you **force-exit** (e.g. Ctrl+C) during `pal pull <path>` (or compatibility alias `pal add`) or `llmli add`:

- **Files on disk:** Unaffected (we never write to your source paths).
- **Chroma:** For that silo we run `delete(where={"silo": ...})` before adding in batches. If you interrupt during the add step, the silo in Chroma may be empty or partially filled.
- **Registry:** Updated only after all batches complete. If you interrupt, the registry is unchanged (stale counts) or the silo was never added.

**What to do:** Re-run `pal pull <path>` (or `pal add <path>`) or `llmli add <path>`. That will delete and re-add that silo cleanly.
