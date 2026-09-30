---
description: Read before touching retrieval, the silo registry, or any code path that enumerates silos — the rules that keep private corpora out of unscoped results.
paths:
  - "src/query/**"
  - "src/state.py"
  - "src/registry_lock.py"
  - "pal_registry.py"
  - "src/file_registry.py"
  - "src/operations_find.py"
  - "mcp_server.py"
type: rule
updated: 2026-09-30
---

# Private silos

A silo with `private: true` in the registry must never appear in a result the
user did not scope. This is a correctness property, not a preference: it exists
because an unscoped `multi_query_knowledge` returned tax and lab-result chunks
unasked.

## The rule

**Naming the silo is the consent signal.** An explicit `silo=<slug>` / `--in
<slug>` reaches a private silo. Everything else must not — including a display
name (`silo="Tax"`), which `run_retrieve` refuses for a private silo because a
model can produce it by guessing.

## What that means when you write code here

- Any Chroma query that runs **without** a caller-supplied silo must carry
  `state.private_filter_clause(db)` in its where-clause. See
  `query/retrieve_locked.py`.
- Any code that reads the **file manifest** across silos must go through
  `file_registry.read_visible_manifest(db, silo=...)`, not `_read_file_manifest`.
  Filenames leak too — tax filenames carry identifiers.
- Any code that **picks a silo on the user's behalf** (scope binding, catalog
  ranking, cross-silo aggregates) must enumerate with `state.list_visible_silos`,
  not `state.list_silos`.
- `state.list_silos` stays the full roster for status, diagnostics, and the UI.
  Knowing a private corpus exists is allowed; pulling from it unasked is not.
  The exception is the lite profile's `silo_roster`: it is that profile's only
  discovery tool, so it omits private slugs entirely and reports a count.
- `private_filter_clause` also excludes `<slug>-artifacts`, where artifact
  compilation writes a silo's derived chunks. A new derived-silo naming scheme
  needs the same treatment.
- `private_silo_slugs` **raises** when the registry is unreadable rather than
  returning `[]`. Do not "fix" that by swallowing the error — an empty list means
  "no private silos", and guessing that on a read failure is the leak.
- Any function that reads the registry, changes it, and writes it back must hold
  `state.registry_transaction(db_path)` across **both** halves. Locking only the
  write still loses updates, and a lost update on `set_silo_private` silently
  un-privates a silo — a privacy regression, not a bookkeeping one. The lock is
  reentrant, so calling another locked function from inside one is fine.
- **A read must never mutate.** `pal._read_llmli_registry` used to run
  `cleanup_stale_registry_entries` on every read; once silo paths were
  canonicalized through symlinks that deleted a live silo's registry entry and
  orphaned its chunks. Cleanup runs from `pal daemon sync`, explicitly.

## When you add a new retrieval or enumeration path

Add a case to `tests/unit/test_silo_privacy.py`. The two that matter are: the
unscoped path carries the `$nin`, and the explicit path does not. A new MCP tool
(either profile) also gets a case in
`tests/integration/test_silo_privacy_mcp_tools.py`, which drives the tool
functions over a real private + public index.

## Deployment note

The macOS app bundle's launcher shims exec this checkout's working tree, so the
resident MCP serves whatever branch is checked out. The flag first shipped on
`main` only; the server ran `dev` for two weeks without it. Keep privacy commits
on every branch that can be checked out here.
