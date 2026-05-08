"""
Output rendering for INTENT_FILENAME_DATE_LOOKUP and the ``llmli find`` CLI.

Two paths:
- ``format_filename_lookup`` — multi-hit list, sorted by name_date desc.
- ``format_filename_lookup_with_excerpt`` — single-hit case; pulls the first
  ~40 lines from Chroma chunks (not raw bytes) so PDFs/DOCX/IPYNB use the same
  text the embeddings saw.

A small ``read_chunks_for_source`` helper handles the Chroma read with sane
fallbacks (no chunks found, Chroma unavailable, etc.).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from query.formatting import shorten_path

EXCERPT_LINE_CAP = 40
EXCERPT_CHAR_CAP = 4000


def format_filename_lookup(
    hits: list[dict[str, Any]],
    *,
    source_label: str,
    no_color: bool,
    range_label: str | None = None,
) -> str:
    """Render a multi-hit (or zero-hit) result as a plain text block."""
    if not hits:
        scope = f" in {source_label}" if source_label else ""
        if range_label:
            return f"No files matched {range_label}{scope}."
        return f"No files matched the date lookup{scope}."

    lines: list[str] = []
    header = (
        f"Matched {len(hits)} file(s)"
        + (f" for {range_label}" if range_label else "")
        + (f" in {source_label}" if source_label else "")
        + "."
    )
    lines.append(header)
    lines.append("")
    for hit in hits:
        lines.append(_format_hit_line(hit))
    return "\n".join(lines)


def format_filename_lookup_with_excerpt(
    hit: dict[str, Any],
    *,
    db_path: str,
    source_label: str,
    no_color: bool,
    range_label: str | None = None,
) -> str:
    """Render a single-hit result with a chunked excerpt."""
    head = (
        f"1 file matched"
        + (f" for {range_label}" if range_label else "")
        + (f" in {source_label}" if source_label else "")
        + ":"
    )
    lines = [head, _format_hit_line(hit)]

    chunks_text, warning = read_chunks_for_source(
        db_path=db_path,
        silo=hit.get("silo") or "",
        source_path=hit.get("path") or "",
    )
    if warning:
        lines.append("")
        lines.append(f"(excerpt unavailable: {warning})")
    elif chunks_text:
        lines.append("")
        lines.append("--- excerpt ---")
        lines.extend(_clamp_excerpt(chunks_text).splitlines())
    return "\n".join(lines)


def _format_hit_line(hit: dict[str, Any]) -> str:
    """Single-line summary for one hit, including any name_date / mtime conflict."""
    path = hit.get("path") or "?"
    short = shorten_path(path)
    name_date = hit.get("name_date")
    mtime_local = hit.get("mtime_local_date") or ""
    parts = [f"  • {short}"]
    if name_date:
        parts.append(f"[name_date={name_date}]")
    if mtime_local and mtime_local != name_date:
        if name_date:
            parts.append(f"(mtime={mtime_local})")
        else:
            parts.append(f"[mtime={mtime_local}]")
    chunk_count = hit.get("chunk_count")
    if chunk_count is not None:
        parts.append(f"chunks={chunk_count}")
    return " ".join(parts)


def read_chunks_for_source(
    *,
    db_path: str,
    silo: str,
    source_path: str,
) -> tuple[str, str | None]:
    """Concatenate chunks for a single (silo, source) ordered by line_start.

    Returns ``(text, warning)``. ``text`` is empty when no chunks exist or an
    error occurs; ``warning`` carries the human-readable cause (or None).
    """
    if not silo or not source_path:
        return "", "missing silo or source"
    try:
        from chroma_client import get_client, release
        from chroma_lock import chroma_shared_lock
        from constants import LLMLI_COLLECTION
    except Exception as e:
        return "", f"chroma import failed: {e}"

    try:
        with chroma_shared_lock(db_path):
            client = get_client(db_path)
            try:
                coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
            except Exception as e:
                return "", f"collection error: {e}"
            try:
                res = coll.get(
                    where={"$and": [{"silo": silo}, {"source": source_path}]},
                    include=["documents", "metadatas"],
                )
            except Exception as e:
                return "", f"chroma query failed: {e}"
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            if not docs:
                return "", None
            paired = list(zip(docs, metas))
            paired.sort(key=lambda dm: _line_sort_key(dm[1]))
            text = "\n\n".join(d for d, _ in paired if d)
            return text, None
    finally:
        try:
            release()
        except Exception:
            pass


def _line_sort_key(meta: dict | None) -> tuple[int, int, int]:
    if not isinstance(meta, dict):
        return (0, 0, 0)
    page = meta.get("page")
    line = meta.get("line_start")
    chunk_index = meta.get("chunk_index")
    return (
        int(page) if isinstance(page, (int, float)) else 0,
        int(line) if isinstance(line, (int, float)) else 0,
        int(chunk_index) if isinstance(chunk_index, (int, float)) else 0,
    )


def _clamp_excerpt(text: str) -> str:
    """Cap excerpt to first N lines / M characters."""
    lines = text.splitlines()
    if len(lines) > EXCERPT_LINE_CAP:
        lines = lines[:EXCERPT_LINE_CAP] + ["…"]
    out = "\n".join(lines)
    if len(out) > EXCERPT_CHAR_CAP:
        out = out[: EXCERPT_CHAR_CAP - 1] + "…"
    return out


def render_range_label(
    date_start: Any,
    date_end: Any,
) -> str | None:
    """Compact human label for a date range, used in headers."""
    if date_start is None and date_end is None:
        return None
    s = str(date_start) if date_start is not None else ""
    e = str(date_end) if date_end is not None else ""
    if s and e and s == e:
        return s
    if s and e:
        return f"{s} to {e}"
    if s:
        return f"on or after {s}"
    return f"on or before {e}"
