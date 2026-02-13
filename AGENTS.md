# AGENTS

Primary source of truth for coding agents in this repo.

If any other document conflicts with this file, follow `AGENTS.md`.

## Purpose

This repository is a local-first CLI tool.
Agent priorities:
1. Keep behavior deterministic and observable.
2. Prefer minimal-friction CLI UX (`pal` first, `llmli` direct when needed).
3. Keep docs and tests aligned with current command behavior.

## Canonical Workflows

### 1) Setup
```bash
uv venv
source .venv/bin/activate
uv sync
```

### 2) Ingest / Pull
```bash
# Day-to-day (recommended)
pal pull /path/to/folder

# Direct tool usage
llmli add /path/to/folder
```

### 3) Query
```bash
# Scoped ask
pal ask --in <silo> "question"

# Unified ask
pal ask --unified "question"

# Direct tool usage
llmli ask --in <silo> "question"
```

### 4) Inspect / Diagnose
```bash
pal ls
pal inspect <silo> --top 20
pal pull --status
pal status
```

### 5) Test
```bash
uv run pytest -q tests/unit
```

## Command Notes

- `pal` is the operator-facing CLI.
- `llmli` is the direct engine CLI.
- `pal sync` refreshes the dev self-silo (`__self__`) when needed.
- Natural shorthand `pal ask in <silo> "..."` is supported and normalized to `--in`.

## Documentation Policy

- Keep docs short and current.
- Remove stale implementation diaries and one-off planning details.
- Prefer behavior contracts over speculative design notes.

## Agent Guardrails

- Run unit tests for touched behavior before finishing.
- Do not add broad config surfaces unless required.
- Keep changes focused and reversible.
