# llmLibrarian (llmli)

**Status:** Rebuilt from recovery. A deterministic, locally-hosted **Context Engine** for high-stakes personal data (taxes, infrastructure, code), designed to be agent-ready but human-controlled.

See **gemini_summary.md** for the full project manifest and recovery roadmap.

## Quick start

```bash
# From project root
python3 -m venv .venv && source .venv/bin/activate  # or Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Ensure Ollama is running and a model is pulled (e.g. llama3.1:8b)
ollama pull llama3.1:8b

# Edit archetypes.yaml: set real paths under each archetype's `folders` (e.g. /tax → /path/to/your/tax)
# Then index an archetype:
python cli.py index --archetype tax

# Ask a question
python cli.py ask --archetype tax "What was my 2021 income?"

# Or index a single folder as a "silo" (unified llmli collection)
python cli.py add /path/to/code
python cli.py ls
```

## CLI commands

| Command | Description |
|--------|-------------|
| `add <path>` | Index folder into unified collection (silo = basename) |
| `ask --archetype <id> <query...>` | Query an archetype's collection via Ollama |
| `ls` | List silos (from `add`) |
| `index --archetype <id>` | Rebuild archetype collection from `archetypes.yaml` folders |
| `rm <silo>` | Remove silo from registry and delete its chunks |
| `log` | Show last add failures |

## Layout (rebuilt)

- **cli.py** — Entrypoint: `python cli.py add|ask|ls|index|rm|log`
- **archetypes.yaml** — Archetypes (tax, infra, palindrome) and limits; set your own `folders` paths.
- **src/** — Librarian code:
  - **embeddings.py**, **load_config.py**, **style.py**, **reranker.py**, **state.py**, **floor.py** — Recreated support modules.
  - **YRvy.py** — Indexing (archetype + add); **WsQD.py** — Query (ask).
  - **indexer.py** / **query.py** — Thin wrappers that re-export from YRvy / WsQD for the CLI.
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
