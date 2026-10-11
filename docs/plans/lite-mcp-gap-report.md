---
description: Empirical lite-vs-full MCP gap audit for small local models (Open WebUI / AnythingLLM), before/after the catalog and privacy fixes.
type: plan
updated: 2026-10-11
globs:
  - mcp_server.py
  - src/mcp_contract.py
  - tests/harness/**
  - tests/contract/test_lite_mcp_catalog.py
---

# Lite MCP gap report

Harness: `uv run python -m tests.harness.lite_mcp_compare`  
Fixtures: `tests/fixtures/lite_harness/` (recipes with YAML frontmatter, dated journal meals, private lab silo).  
Contract: `tests/contract/test_lite_mcp_catalog.py`.

## Catalog size

| Profile | Tools (before) | Schema (before) | Tools (after) | Schema (after) |
|---------|----------------|-----------------|---------------|----------------|
| full | 20 | 28,402 B (~7100 tok) | 20 (unchanged) | 28,402 B |
| lite | 4 | 3,972 B (~993 tok) | 5 | 7,096 B (~1774 tok; budget &lt;12 KB) |

Lite tools after: `silo_roster`, `retrieve_knowledge`, `find_files`, `read_document`, `ask_image`.

## Task results (before → after)

| Task | Full | Lite before | Lite after | Fix |
|------|------|-------------|------------|-----|
| Filtered recipe (`main_protein=bison`, `category=dinner`, `max_total_time=60`) | pass | **fail** — no filter params; metadata dropped | pass | optional frontmatter filters on `retrieve_knowledge`; compact keeps `metadata` / `mtime_iso` |
| Meal on `2026-10-10` | pass (`find_files`) | **fail** — no date listing | pass | `find_files` in lite catalog |
| Last 7 days meals | pass | **fail** — impossible | pass | same |
| Read cited recipe | pass | pass | pass | already had `read_document` |
| `ask_image` empty-match guidance | pass | **fail** — recommended `find_files` (missing) | pass | `find_files` in catalog |
| Private refusal + allow-list | pass (named/none) | **fail** — no `PRIVATE_READS_ALLOW` | pass | `LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW` on both endpoints; allow-listed private slugs appear on `silo_roster` |
| `recommended_action` lite-safe | pass | pass | pass | lite empty results may now suggest `find_files` when a year is present |
| Compact metadata for citation | pass | **fail** | pass | `_compact_lite_chunk` keeps frontmatter + dates |
| Multi-angle survey | pass (`multi_query`) | pass via sequential `retrieve_knowledge` | pass (same) | **deferred** — keep catalog small; sequential calls work |

## Design choices

1. **Filters as optional params**, not a new tool — same surface as `query_personal_knowledge`.
2. **`find_files` added** (5th tool) rather than overloading retrieval for date listing.
3. **No lite `multi_query_knowledge`** — sequential retrieves stay within the size budget.
4. **Allow-list applies to full and lite** — `named` + allow = only those private slugs; `none` + allow = selected override.
5. **Full profile behavior unchanged** except allow-list filtering of roster rows when configured.

## Operator knobs

```bash
# Lite mount beside full (Open WebUI / AnythingLLM)
LLMLIBRARIAN_MCP_LITE_PATH=/lite
LLMLIBRARIAN_MCP_LITE_PRIVATE_READS=none   # default
LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW=journal-slug   # optional selective private

# Or a dedicated lite process
LLMLIBRARIAN_MCP_PROFILE=lite
```
