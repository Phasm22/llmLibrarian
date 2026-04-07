# AGENTS

Primary source of truth for coding agents in this repo.

If any other document conflicts with this file, follow `AGENTS.md`.

## Purpose

This repository is a local-first CLI tool.
Agent priorities:
1. Keep behavior deterministic and observable.
2. Prefer **MCP tools** when the llmLibrarian MCP server is available; otherwise minimal-friction CLI (`pal` first, `llmli` direct when needed).
3. Keep docs and tests aligned with current command behavior.

## Agent operations (MCP-first)

When integrated via MCP, prefer tools over shell so you do not depend on `PYTHONPATH` or CLI flag drift:

1. **`health`** — DB presence, embedding stack, basic sanity (call first if tools misbehave).
2. **`list_silos`** / **`capabilities`** — discover slugs and supported file types.
3. **`retrieve`** / **`retrieve_bulk`** — document Q&A from indexed data.
4. **`add_silo`** — index a **file or directory** (same rules as `llmli add`).
5. **`trigger_reindex`** — incremental refresh for a registered silo; **`repair_silo`** — hard reset when Chroma/registry is inconsistent.

Use **`pal`** / **`llmli`** only when MCP is unavailable or you need flags not exposed on tools (e.g. niche `llmli` options).

**Entry-point behavior** (CLI vs MCP vs `pal pull` all vs `ensure_self_silo`): [docs/orchestration-matrix.md](docs/orchestration-matrix.md). Shared implementation: `orchestration.ingest.run_ingest` → `ingest.run_add`.

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
