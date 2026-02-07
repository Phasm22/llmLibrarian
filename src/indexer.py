"""
Archetype-aware ingest: one collection per archetype, rebuild from scratch.
Resource limits, ZIP limits (skip encrypted), include/exclude via should_index.
Metadata: source_path, mtime, chunk_hash, line_start (code), page (PDF).

Flow: collect file list -> read+chunk in ThreadPoolExecutor -> batch add().
ZIPs processed in main thread (limits); regular files in parallel; add in batches.
"""
# Re-export from ingest core so cli can import from indexer
from ingest import (
    run_index,
    run_add,
    DB_PATH,
    LLMLI_COLLECTION,
    ADD_DEFAULT_INCLUDE,
    ADD_DEFAULT_EXCLUDE,
)

__all__ = [
    "run_index",
    "run_add",
    "DB_PATH",
    "LLMLI_COLLECTION",
    "ADD_DEFAULT_INCLUDE",
    "ADD_DEFAULT_EXCLUDE",
]
