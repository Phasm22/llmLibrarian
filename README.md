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
| `pal add <path>` | llmli add + register source in ~/.pal |
| `pal ask ["question"]` | llmli ask (unified); use `--in <silo>` to scope |
| `pal ls` | llmli ls |
| `pal log` | llmli log --last |
| `pal tool llmli <args...>` | Passthrough to llmli |

## llmli (librarian CLI)

| Command | Description |
|--------|-------------|
| `add <path>` | Index folder into unified collection (silo = slug of basename) |
| `ask [--archetype \<id\> \| --in \<silo\>] <query...>` | Query: default = unified llmli; `--in` = one silo; `--archetype` = archetype collection |
| `ls` | List silos: `Display Name (slug)` + path, files, chunks |
| `index --archetype <id>` | Rebuild archetype collection from `archetypes.yaml` folders |
| `rm <silo>` | Remove silo (slug or display name) and delete its chunks |
| `log [--last]` | Show last add failures |

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

## Next steps (from manifest)

1. Set real paths in **archetypes.yaml** for `/tax`, `/infra`, `/palindrome`.
2. Re-index: `llmli index --archetype tax` (and infra/palindrome as needed).
3. Add macOS terminal craftiness (OSC 8 file links, semantic marks) if desired.
4. Add `--json` to `ask` for agent consumption.
5. Create **gold_standard.json** for tax verification.
