"""Read one indexed file back out of the index, in order, a page at a time.

Serves the MCP read_document tool. It reads the chunks the index already holds
for a source — never the file on disk — so it cannot become an arbitrary local
file read, and PDFs/images come back as their extracted text. The caller resolves
the path through the privacy-aware manifest first (mcp_server._resolve_indexed_file).
"""
from __future__ import annotations

from typing import Any

MIN_CHARS = 500
MAX_CHARS = 20000
_PAGE_SIZE = 500
_MAX_OVERLAP = 400


def _order_key(meta: dict[str, Any]) -> tuple:
    def _num(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("inf")

    return (_num(meta.get("page")), _num(meta.get("chunk_index")), _num(meta.get("line_start")))


def trim_overlap(previous: str, following: str, max_overlap: int = _MAX_OVERLAP) -> str:
    """Drop the head of ``following`` that repeats the tail of ``previous``.

    Chunks are cut with overlap (LLMLIBRARIAN_CHUNK_OVERLAP), so a naive join
    repeats up to a few hundred characters at every seam.
    """
    limit = min(max_overlap, len(previous), len(following))
    for size in range(limit, 19, -1):
        if previous.endswith(following[:size]):
            return following[size:]
    return following


def read_indexed_document(
    collection: Any,
    *,
    silo: str,
    source: str,
    start_chunk: int = 0,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """Join a source's chunks in document order from ``start_chunk`` up to ``max_chars``."""
    max_chars = max(MIN_CHARS, min(MAX_CHARS, int(max_chars)))
    start_chunk = max(0, int(start_chunk))
    docs: list[str] = []
    metas: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = collection.get(
            where={"$and": [{"silo": silo}, {"source": source}]},
            include=["documents", "metadatas"],
            limit=_PAGE_SIZE,
            offset=offset,
        )
        page_docs = page.get("documents") or []
        page_metas = page.get("metadatas") or []
        docs.extend(page_docs)
        metas.extend(m or {} for m in page_metas)
        if len(page_docs) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    ordered = sorted(zip(docs, metas), key=lambda pair: _order_key(pair[1]))
    total = len(ordered)
    parts: list[str] = []
    used = 0
    index = start_chunk
    previous = ""
    while index < total:
        text = trim_overlap(previous, ordered[index][0] or "")
        if parts and used + len(text) > max_chars:
            break
        parts.append(text)
        used += len(text)
        previous = ordered[index][0] or ""
        index += 1

    text = "".join(parts)
    truncated = False
    if len(text) > max_chars:
        # A single chunk larger than the budget: cut it, and resume at the next.
        text = text[:max_chars]
        truncated = True
    out: dict[str, Any] = {
        "source": source,
        "silo": silo,
        "total_chunks": total,
        "start_chunk": start_chunk,
        "end_chunk": index,
        "text": text,
    }
    pages = sorted({m.get("page") for _d, m in ordered[start_chunk:index] if m.get("page") is not None})
    if pages:
        out["pages"] = [pages[0], pages[-1]]
    if index < total:
        out["next_start_chunk"] = index
    if truncated:
        out["truncated_chunk"] = True
    return out
