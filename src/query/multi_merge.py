"""Merge per-query chunk lists for multi_query_knowledge and `llmli multi-query`."""
from __future__ import annotations

import hashlib
from typing import Any

MERGE_RRF_K = 60


def chunk_identity(chunk: dict[str, Any]) -> str | None:
    """Full-text hash: equals ingest's chunk_hash, so two queries agree on one chunk.

    Ingest never writes chunk_index for text chunks, so (silo, source, chunk_index)
    would collapse every chunk of a file into one.
    """
    text = chunk.get("text") or ""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def merge_query_results(
    per_query: list[tuple[str, list[dict[str, Any]]]],
    max_total_chunks: int,
    *,
    reranked: bool = False,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Return (chunks, truncated, method); each chunk is a tagged copy.

    reranked=False: score sort, the legacy order — per-query order already follows
    distance, so sorting by the distance-derived score keeps it.
    reranked=True: reciprocal-rank fusion over each query's order. A cross-encoder
    order is not a distance order, so a score sort would undo it, and its logits
    are not comparable across queries; ranks are. ``query`` names the query that
    ranked the chunk best (earliest query on ties).
    """
    rows: dict[str, dict[str, Any]] = {}
    for q_index, (query, chunks) in enumerate(per_query):
        for rank, chunk in enumerate(chunks, start=1):
            key = chunk_identity(chunk)
            if key is None:
                continue
            row = rows.get(key)
            if row is None:
                row = rows[key] = {"chunk": chunk, "fused": 0.0, "best": (rank, q_index), "query": query, "first": len(rows)}
            elif reranked and (rank, q_index) < row["best"]:
                row.update(chunk=chunk, best=(rank, q_index), query=query)
            row["fused"] += 1.0 / (MERGE_RRF_K + rank)
    if reranked:
        ranked = sorted(rows.values(), key=lambda r: (-r["fused"], r["best"]))
        method = "rrf"
    else:
        ranked = sorted(rows.values(), key=lambda r: (-(r["chunk"].get("score") or 0), r["first"]))
        method = "score_sort"
    merged = [{**r["chunk"], "query": r["query"]} for r in ranked]
    truncated = len(merged) > max_total_chunks
    return merged[:max_total_chunks], truncated, method
