# AGENTS

This repo supports multiple coding agents (Claude, Cursor, Codex). Each agent uses its own config directory. This file documents common workflows and commands that apply across agents.

## Workflows

### 1) Setup
- Create a virtual environment and install dependencies.
- Verify the CLI runs locally.

### 2) Ingest
- Prepare inputs.
- Run ingest.
- Validate output.

### 3) Query
- Start the query engine.
- Run a sample query.
- Verify formatting output.

### 4) Tests
- Run unit tests before committing.
- If adding features, include or update tests.

### 5) Lint/Format
- Run formatting and linting (if configured).
- Keep diffs minimal and focused.

## Commands

### Environment
```bash
uv venv
uv pip install -r requirements.txt
```

### Ingest
```bash
python -m src.ingest
```

### Query
```bash
python -m src.query_engine
```

### Tests
```bash
uv run pytest -q
python -m pytest -q
```

## Notes
- Each agent may have additional instructions in its own directory/config.
- Keep commands updated if entry points or tooling change.
- In this repo, `pal` maintains a dev-only self-silo (`__self__`). If the repo changes since the last self index, it warns: `Self-silo stale (repo changed since last index). Run pal ensure-self.`
