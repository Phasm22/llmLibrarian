# MCP Discovery Verbosity Issue

## Summary

`list_silos` and `session_context` currently return enough metadata to be useful for a full-size assistant, but the payload is too large for small local models with an 8k context window. Retrieval itself is not the main problem: scoped `query_personal_knowledge` works when the caller already knows the exact silo slug. The friction is that the model often needs discovery first, and discovery can consume most or all of the available context before the model gets to reason over retrieved chunks.

## Observed behavior

On the local Mac database, discovery is already sizable:

- `list_silos`: about 5.7k JSON characters with 5 silos
- `session_context`: about 7.5k JSON characters with 5 silos
- scoped `query_personal_knowledge(..., n_results=5)`: about 6.3k-7.9k JSON characters

On Palindrome, discovery is much heavier:

- `list_silos`: about 19.9k JSON characters with 20 silos
- `session_context`: about 22.6k JSON characters with 20 silos
- scoped `query_personal_knowledge(..., n_results=5)`: about 6.5k-6.8k JSON characters

For an 8k-context local model, this makes the recommended bootstrap flow fragile. The model may spend its budget reading a roster instead of using the actual retrieval evidence.

## Why direct slug queries help

If the caller already knows the exact silo slug, it can skip discovery and call:

```text
query_personal_knowledge(query, silo="<exact-slug>", n_results=...)
```

That path retrieves normally and avoids the large roster payload. This is why the tool can appear "fine" when manually tested with the right slug.

The catch is that slugs are machine-local and hash-suffixed. A slug that works on one host may not work on another. For example, the local Mac repo silo and the Palindrome repo silo had different exact slugs. Display-like names or stale remembered slugs can return zero chunks without indicating that retrieval is broken.

## Root cause

The discovery tools return full operational metadata by default:

- full paths
- file counts
- chunk counts
- update timestamps
- doc type breakdowns
- exclude patterns
- ingest failure state
- index error state
- staleness fields when requested
- host/private fields on newer versions
- health summary and recommended actions in `session_context`

That is useful for diagnostics, but too verbose for the common small-model path: "Which silo should I query?"

## Recommended fix

Add a compact discovery mode or a separate lightweight roster tool.

Suggested shape:

```json
{
  "db_exists": true,
  "silo_count": 20,
  "silos": [
    {
      "slug": "llmlibrarian-4fceb97e",
      "display_name": "llmLibrarian",
      "chunks_count": 2318,
      "private": false,
      "host": "palindrome"
    }
  ],
  "excluded_private_silos": []
}
```

Fields to omit from compact mode unless explicitly requested:

- source path
- exclude patterns
- doc type breakdown
- per-silo ingest failure lists
- detailed query health
- full staleness metadata
- verbose scope policy prose

Possible implementation options:

1. Add `compact: bool = False` to `list_silos`.
2. Add `compact: bool = False` to `session_context`.
3. Add a new `list_silo_slugs` or `silo_roster` tool that always returns the minimal roster.

The safest user-facing path is probably option 3. It avoids changing existing diagnostics contracts while giving small models a clear low-token discovery route.

## Tool-doc guidance change

The current docs correctly tell callers not to infer a silo's topic from its slug alone, but they push all callers toward full `list_silos` before retrieval. For small models, the tool descriptions should mention the compact route once it exists:

```text
If you only need to choose a silo on a small-context model, call silo_roster first.
Use full list_silos/session_context only for diagnostics, staleness checks, or ambiguous scope.
```

## Expected result

Small local models should be able to:

1. Fetch a compact roster.
2. Pick the exact machine-local slug.
3. Run scoped retrieval.
4. Spend most of the context window on returned chunks instead of roster metadata.

This keeps the current deterministic retrieval behavior while making the MCP surface more usable under tight context budgets.
