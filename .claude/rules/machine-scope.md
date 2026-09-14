---
description: Read when writing docs, runbooks, or defaults that mention paths, ports, or silos — this repo runs on two machines with different filesystems.
paths:
  - "**/*.md"
  - ".mcp.json"
  - "src/constants.py"
type: rule
updated: 2026-09-14
---

# This repo runs on two machines

**Linux PC** (prod, `pc-stacks`, MCP on 8765) and **MacBook** (dev, launchd
agents, MCP on 8766). Separate filesystems, separate `LLMLIBRARIAN_DB`, separate
registries. The Obsidian vault syncs between them; the index does not.

## Rules

1. **No absolute home directories in committed files.** `.mcp.json` hardcoded
   `/home/tj/Desktop/llmLibrarian` and broke every Mac session with `ENOENT`.
   Use `${LLMLIBRARIAN_HOME:-.}` / `$LLMLIBRARIAN_HOME`.
2. **Label machine-specific instructions.** If a command only works on one box
   (`pc-stacks`, launchctl, a port number), say which. An unlabeled command reads
   as universal and fails silently on the other machine.
3. **Never write a silo roster into a doc.** Rosters drift and are per-machine.
   Document the *policy*; instruct the reader to call `list_silos`.
4. **Defaults must be real.** `llama3.1:8b` was the documented default for months
   and was never pulled on either box. If you write a default, verify it exists.
