# llmLibrarian (llmli)

**Status:** Rebuilt from recovery. A deterministic, locally-hosted **Context Engine** for high-stakes personal data (taxes, infrastructure, code), designed to be agent-ready but human-controlled.

See **gemini_summary.md** for the full project manifest and recovery roadmap.

## Quick start

```bash
# From project root (use uv for venv + deps)
uv venv && source .venv/bin/activate   # or Windows: .venv\Scripts\activate
uv sync

# Ensure Ollama is running and a model is pulled (e.g. llama3.1:8b)
ollama pull llama3.1:8b

# Option A: Agent CLI (pal) — orchestrates llmli; state in ~/.pal/registry.json
pal add /path/to/code
pal ask "what did I write about X?"
pal ls
pal log
pal tool llmli ask --in my-silo "..."   # passthrough

# Option B: Use llmli directly
llmli add /path/to/code
llmli ask "what is in my docs?"         # default: unified collection (all silos)
llmli ask --in tax "what forms?"        # scoped to silo (slug or display name)
llmli ask --archetype tax "..."         # archetype collection from archetypes.yaml
llmli ls
llmli rm job-related-stuff             # slug or display name
llmli log --last

# Archetypes (optional): set paths in archetypes.yaml, then
llmli index --archetype tax
llmli ask --archetype tax "What was my 2021 income?"
```

## pal (agent CLI)

**pal** is the control-plane: it delegates to **llmli** and keeps a small registry in `~/.pal/registry.json`. Use `pal` for daily workflows; use `llmli` directly when you need full options.

| Command | Description |
|--------|-------------|
| `pal add <path> [--allow-cloud]` | llmli add + register in ~/.pal; cloud paths blocked unless `--allow-cloud` |
| `pal ask ["question"]` | llmli ask (unified); use `--in <silo>` to scope |
| `pal ls` | llmli ls |
| `pal inspect <silo>` | llmli inspect: path, total chunks, per-file chunk counts |
| `pal log` | llmli log --last |
| `pal tool llmli <args...>` | Passthrough to llmli |

## llmli (librarian CLI)

| Command | Description |
|--------|-------------|
| `add <path> [--allow-cloud]` | Index folder (silo = slug of basename). **Cloud-sync paths** (OneDrive, iCloud, Dropbox, Google Drive) are **blocked** by default; use `--allow-cloud` only if you’ve pinned/fully downloaded (ingestion can be unreliable otherwise). |
| `ask [--archetype \<id\> \| --in \<silo\>] <query...>` | Query: default = unified llmli; `--in` = one silo; `--archetype` = archetype collection |
| `ls` | List silos: `Display Name (slug)` + path, files, chunks |
| `inspect <silo>` | Silo details: path, total chunks, and per-file chunk counts (from store) |
| `index --archetype <id>` | Rebuild archetype collection from `archetypes.yaml` folders |
| `rm <silo>` | Remove silo (slug or display name) and delete its chunks |
| `log [--last]` | Show last add failures |

## Testing it / Edge cases to try

The goal is to remove friction between *“I need this information”* and finding it in your file system when you don’t know exactly where it is—and for it to still help when you do know.

**Worth trying:**

- **Vague / “I think I wrote something about…”** — Ask in natural language across all silos; see if the right doc shows up and the snippet is enough to recognize it. Click the source link to open the file.
- **You know the topic but not the file** — e.g. “what did I decide about the API for X?” or “where did I list the 2022 classes?” — Ask; check that the answer and sources point to the right place.
- **You know roughly where (silo/folder)** — Use `pal ask --in school "..."` or `--in tax "..."` to limit to one silo and confirm results stay scoped.
- **Mixed content** — Silo with both code and docs (e.g. README + .docx + .pdf). Ask a doc-style question and a code-style question; sources should include both when relevant, and links should open in the right app (editor vs Word/Preview).
- **After adding new stuff** — Add a folder, then ask about something only in that folder; confirm it appears. Use `pal ls` and `pal log` to see state and any add failures.
- **Wrong or empty silo** — Ask `--in` a silo that doesn’t have the answer; you should get “no indexed content” or an answer that says it’s not there, not a hallucinated path.

If something breaks or feels wrong in these situations, that’s the kind of edge case worth fixing.

## Development setup (2025–2026)

Plain **venv** is still fine, but for a small project like this the better default is **uv** (or **rye**): one tool for create-venv, install, lockfile, and run. This repo uses **uv**:

- `uv venv` — create `.venv` (no need to pick a Python; uv finds it)
- `uv sync` — install from `pyproject.toml` and lock
- `uv run llmli ls` — run without activating the venv

Alternatives: **rye** is similar (init, sync, run). **pip + venv** works but is slower and has no lockfile. **Poetry** is heavier than needed for a single-app project.

## Layout (rebuilt)

- **pyproject.toml** — Project + deps; **uv** for venv and install (`uv venv`, `uv sync`).
- **cli.py** — llmli entrypoint: `llmli add|ask|ls|index|rm|log`
- **pal.py** — Agent CLI: `pal add|ask|ls|log|tool`; state in `~/.pal/registry.json`.
- **archetypes.yaml** — Archetypes (tax, infra, palindrome) and limits; set your own `folders` paths.
- **src/** — Librarian code:
  - **embeddings.py**, **load_config.py**, **style.py**, **reranker.py**, **state.py**, **floor.py** — Recreated support modules.
  - **ingest.py** — Indexing core (archetype + add); **query_engine.py** — Query (ask).
  - **indexer.py** / **query.py** — Thin wrappers that re-export from ingest / query_engine for the CLI.
- **gemini_summary.md** — Project manifest and recovery notes.

## Env (optional)

- `LLMLIBRARIAN_DB` — DB path (default: `./my_brain_db`)
- `LLMLIBRARIAN_CONFIG` — Path to `archetypes.yaml`
- `LLMLIBRARIAN_MODEL` — Ollama model (default: `llama3.1:8b`)
- `LLMLIBRARIAN_LOG=1` — Enable indexing log to `llmlibrarian_index.log`
- `LLMLIBRARIAN_RERANK=1` — Enable reranker (requires `sentence-transformers`)
- `LLMLIBRARIAN_EDITOR_SCHEME` — URL scheme for source links: `vscode` (default) or `cursor` so Cmd+click opens file at line in that editor; `file` for plain `file://` links

## Chunking & scaling (tuning)

Defaults are chosen for a mix of code and docs; you can tune via env or config.

- **Chunking:** 1000 chars, 100 overlap (line-aware). Good for semantic search; smaller chunks (e.g. 500) can improve precision for code. Set `LLMLIBRARIAN_CHUNK_SIZE` (100–4000) and `LLMLIBRARIAN_CHUNK_OVERLAP` (0–half of size) to override.
- **Parallelism:** File read + chunking use a thread pool (default workers = min(8, cpu_count); max 32). Embedding runs in the main thread in batches, so the bottleneck is usually Chroma’s embedding. Set `LLMLIBRARIAN_MAX_WORKERS` to cap or increase file-level parallelism.
- **Batch size:** 256 chunks per `add()` call (embedding batch). Larger = fewer round-trips, more memory. Set `LLMLIBRARIAN_ADD_BATCH_SIZE` (1–2000) for slower machines or bigger batches.
- **Limits** (in `archetypes.yaml` or config): `max_file_size_mb`, `max_depth`, `max_archive_size_mb`, `max_files_per_zip` control how much of a tree is indexed. ZIPs are processed sequentially to avoid memory spikes.

So: chunking and scaling are in a good place by default; use the env vars above if you need to scale up (bigger batches, more workers) or down (smaller batches, fewer workers, or smaller chunks for code-heavy silos).
