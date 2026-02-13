# Chroma and Stack Notes (Current)

High-level only.

## Current Stack

- Vector store: ChromaDB (persistent local collection)
- Ingest: local file processors + chunking + metadata registry/manifest
- Query: intent routing + deterministic guardrails + retrieval + optional LLM fallback
- CLI: `pal` (operator), `llmli` (direct)

## Why This Stays Minimal

This file intentionally avoids deep design narrative.
If a detail is not an active runtime contract, it does not belong here.

For runtime behavior details, use `docs/TECH.md`.
For agent workflow and command truth, use `AGENTS.md`.
