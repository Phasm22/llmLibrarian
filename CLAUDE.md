# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

llmLibrarian (llmli) is a deterministic, locally-hosted RAG context engine for personal data. It indexes folders into "silos" (ChromaDB collections), then answers questions using retrieved chunks + Ollama LLM. The design is intentionally stateless—no chat memory, no evolving agent persona. Every answer derives entirely from indexed data + current query.

## Development Commands

```bash
# Setup
uv venv && source .venv/bin/activate
uv sync
ollama pull llama3.1:8b

# Run CLI commands
llmli add <folder>              # Index folder (silo = folder basename)
llmli ask "question"            # Query all silos
llmli ask --in <silo> "query"   # Query specific silo
llmli ls                        # List silos
llmli inspect <silo>            # Silo details + per-file chunk counts
llmli rm <silo>                 # Remove silo
llmli capabilities              # Supported file types

# pal is the agent layer (delegates to llmli; state in ~/.pal/registry.json)
pal add <folder>
pal ask "question"
pal ask --in <silo> "query"
```

## Architecture

**Two CLIs:**
- `cli.py` → `llmli` — Full control CLI (add, ask, ls, inspect, index, rm, capabilities, log)
- `pal.py` → `pal` — Agent CLI that orchestrates llmli; maintains state in `~/.pal/registry.json`

**Core pipeline (src/):**
- `constants.py` — Centralized shared constants (DB_PATH, LLMLI_COLLECTION, chunk/query defaults)
- `processors.py` — Document processor abstraction (Protocol + PDFProcessor, DOCXProcessor, XLSXProcessor, PPTXProcessor, TextProcessor)
- `ingest.py` — Indexing core: file collection, chunking, batch add to ChromaDB (uses processors for extraction)
- `query/` — Query pipeline package (split from former query_engine.py):
  - `intent.py` — Intent routing constants and `route_intent()`
  - `retrieval.py` — Vector + hybrid search (RRF), diversity, dedup, sub-scope resolution
  - `context.py` — Recency scoring, timing detection, context block formatting for LLM prompt
  - `formatting.py` — Answer styling, source linkification, path shortening
  - `code_language.py` — Deterministic language detection by file extension count
  - `trace.py` — JSON-lines trace logging
  - `core.py` — `run_ask()` orchestration (ties all modules together)
- `state.py` — Silo registry and failure tracking (JSON files in DB path)
- `embeddings.py` — ChromaDB embedding function (default: ONNX + all-MiniLM-L6-v2)
- `reranker.py` — Optional cross-encoder reranker (env: LLMLIBRARIAN_RERANK=1)

**Testing:**
- `tests/fixtures/sample_silo/` — Sample files (md, txt, py, json) for quick add/ask testing

**Data flow:**
1. `add` → collect files → chunk (line-aware, 1000 chars, 100 overlap) → extract text (pymupdf/python-docx/openpyxl/python-pptx) → embed → ChromaDB
2. `ask` → intent route → retrieve from ChromaDB (with optional silo filter) → diversify (max 4 chunks/file) → optional rerank → build prompt → Ollama → styled output with sources

**Storage:**
- ChromaDB persistent client in `./my_brain_db` (or `LLMLIBRARIAN_DB`)
- Single collection `llmli` for all silos; chunks tagged with `silo` metadata
- Registry files: `llmli_registry.json`, `llmli_file_registry.json`, `llmli_last_failures.json`

**Intent routing (silent, no CLI flags):**
- `LOOKUP` — Default factual queries
- `EVIDENCE_PROFILE` — "What do I like", preferences (triggers hybrid search with RRF)
- `AGGREGATE` — "All my income", totals across docs
- `CODE_LANGUAGE` — "Most common language" (deterministic count by extension)
- `CAPABILITIES` — "What file types" (returns static report, no RAG)

## Key Environment Variables

- `LLMLIBRARIAN_DB` — DB path (default: `./my_brain_db`)
- `LLMLIBRARIAN_MODEL` — Ollama model (default: `llama3.1:8b`)
- `LLMLIBRARIAN_TRACE` — JSON-lines trace file for debugging
- `LLMLIBRARIAN_RERANK=1` — Enable cross-encoder reranker
- `LLMLIBRARIAN_CHUNK_SIZE` / `LLMLIBRARIAN_CHUNK_OVERLAP` — Tuning (default: 1000/100)

## Configuration

`archetypes.yaml` defines prompts and optional index configs:
- **Prompt-only archetypes:** Add silo with matching slug, then `ask --in <silo>` uses that prompt
- **Full archetypes:** Include `folders`, `include`/`exclude`, `collection` for `llmli index --archetype X`

## File Type Support

First-class extraction: PDF (pymupdf), DOCX (python-docx), XLSX (openpyxl), PPTX (python-pptx), plus text/code files. ZIP archives are processed for embedded documents. Cloud-sync paths (OneDrive, iCloud, Dropbox) blocked by default—use `--allow-cloud` to override.
