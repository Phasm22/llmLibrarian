"""Read one indexed file back out of the index, in order, a page at a time.

Serves the MCP read_document tool. It reads the chunks the index already holds
for a source — never the file on disk — so it cannot become an arbitrary local
file read, and PDFs/images come back as their extracted text. The caller resolves
the path through the privacy-aware manifest first (mcp_server._resolve_indexed_file).
"""
from __future__ import annotations

import hashlib
from typing import Any

MIN_CHARS = 500
MAX_CHARS = 20000
_PAGE_SIZE = 500
# The chunker keeps whole lines in the overlap, so a seam can be as long as one
# max-size chunk (LLMLIBRARIAN_CHUNK_SIZE caps at 4000), not the 150-char target.
_MAX_OVERLAP = 8000
_CURSOR_VERSION = "c1"
_ANCHOR_VERSION = "a1"


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


def fetch_ordered_chunks(collection: Any, *, silo: str, source: str) -> list[tuple[str, dict[str, Any]]]:
    """Every chunk of one source, in document order."""
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
    return sorted(zip(docs, metas), key=lambda pair: _order_key(pair[1]))


def seam_length(ordered: list[tuple[str, dict[str, Any]]], index: int) -> int:
    """Characters at the head of chunk ``index`` that repeat the end of chunk ``index - 1``."""
    if index <= 0:
        return 0
    raw = ordered[index][0] or ""
    return len(raw) - len(trim_overlap(ordered[index - 1][0] or "", raw))


def _cut_point(text: str, limit: int) -> int:
    """Where to cut ``text`` to fit ``limit``: a newline or space in the last fifth if any."""
    if len(text) <= limit:
        return len(text)
    floor = int(limit * 0.8)
    for sep in ("\n", " "):
        at = text.rfind(sep, floor, limit)
        if at >= floor:
            return at + 1
    return limit


def read_span(
    ordered: list[tuple[str, dict[str, Any]]],
    start_chunk: int,
    start_char: int,
    max_chars: int,
    *,
    trim_leading_seam: bool = True,
) -> tuple[str, int, int, bool]:
    """Seam-trimmed text from (start_chunk, start_char) up to ``max_chars``.

    Returns (text, end_chunk, end_char, truncated): the next unread character is
    ``end_char`` into chunk ``end_chunk``. Pages end on a chunk boundary unless a
    single chunk is larger than the budget; then it is cut and end_char > 0.
    ``trim_leading_seam=False`` keeps the first chunk whole: right when the
    preceding chunk was not shown, wrong for a page that follows another.
    """
    parts: list[str] = []
    used = 0
    index, char = start_chunk, start_char
    while index < len(ordered):
        raw = ordered[index][0] or ""
        seam = seam_length(ordered, index) if (parts or trim_leading_seam) else 0
        begin = max(char, seam)
        piece = raw[begin:]
        if parts and used + len(piece) > max_chars:
            break
        if used + len(piece) > max_chars:
            cut = _cut_point(piece, max_chars - used)
            parts.append(piece[:cut])
            return "".join(parts), index, begin + cut, True
        parts.append(piece)
        used += len(piece)
        index, char = index + 1, 0
    return "".join(parts), index, 0, False


def document_fingerprint(ordered: list[tuple[str, dict[str, Any]]]) -> str:
    """Changes whenever the source is reindexed with different chunks."""
    h = hashlib.sha1()
    for doc, meta in ordered:
        h.update(str(meta.get("chunk_hash") or hashlib.sha1((doc or "").encode()).hexdigest()).encode())
        h.update(b"\x1f")
    return h.hexdigest()[:10]


def encode_cursor(chunk: int, char: int, fingerprint: str) -> str:
    return f"{_CURSOR_VERSION}.{chunk}.{char}.{fingerprint}"


def decode_cursor(cursor: str) -> tuple[int, int, str] | None:
    parts = str(cursor or "").strip().split(".")
    if len(parts) != 4 or parts[0] != _CURSOR_VERSION:
        return None
    try:
        chunk, char = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if chunk < 0 or char < 0:
        return None
    return chunk, char, parts[3]


def anchor_cursor(chunk_hash: str) -> str:
    """A cursor that opens read_document one chunk before the chunk with this hash."""
    return f"{_ANCHOR_VERSION}.{chunk_hash}"


def read_indexed_document(
    collection: Any,
    *,
    silo: str,
    source: str,
    start_chunk: int = 0,
    cursor: str | None = None,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """Join a source's chunks in document order from ``cursor`` (or ``start_chunk``) up to ``max_chars``."""
    max_chars = max(MIN_CHARS, min(MAX_CHARS, int(max_chars)))
    start_chunk = max(0, int(start_chunk))
    start_char = 0
    ordered = fetch_ordered_chunks(collection, silo=silo, source=source)
    total = len(ordered)
    fingerprint = document_fingerprint(ordered)
    anchor: int | None = None
    if cursor and str(cursor).startswith(f"{_ANCHOR_VERSION}."):
        wanted = str(cursor).split(".", 1)[1]
        anchor = next((i for i, (_d, m) in enumerate(ordered) if m.get("chunk_hash") == wanted), None)
        if anchor is None:
            return {
                "source": source,
                "silo": silo,
                "error": "That passage is not in this file's index (a different file, or reindexed since).",
                "cursor_stale": True,
                "text": "",
            }
        start_chunk = max(0, anchor - 1)
    elif cursor:
        decoded = decode_cursor(cursor)
        if decoded is None:
            return {"source": source, "silo": silo, "error": "cursor is not one read_document returned", "text": ""}
        start_chunk, start_char, cursor_fp = decoded
        if cursor_fp != fingerprint or start_chunk > total:
            return {
                "source": source,
                "silo": silo,
                "error": "The file was reindexed since this cursor was issued; its chunks moved.",
                "cursor_stale": True,
                "text": "",
            }
    text, index, char, truncated = read_span(
        ordered, start_chunk, start_char, max_chars, trim_leading_seam=anchor is None
    )
    out: dict[str, Any] = {
        "source": source,
        "silo": silo,
        "total_chunks": total,
        "start_chunk": start_chunk,
        "end_chunk": index,
        "text": text,
    }
    if start_char:
        out["start_char"] = start_char
    if anchor is not None:
        out["anchor_chunk"] = anchor
    covered = ordered[start_chunk : index + (1 if char else 0)]
    pages = sorted({m.get("page") for _d, m in covered if m.get("page") is not None})
    if pages:
        out["pages"] = [pages[0], pages[-1]]
    if index < total:
        out["next_cursor"] = encode_cursor(index, char, fingerprint)
        if not char:
            # Chunk-aligned, so the legacy integer resumes losslessly too. Mid-chunk
            # it would either skip the remainder or re-read the same head forever.
            out["next_start_chunk"] = index
    if truncated:
        out["truncated_chunk"] = True
    return out
