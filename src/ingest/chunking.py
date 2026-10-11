"""Markdown-aware and size-based text chunking."""
from __future__ import annotations

import os
import re
from typing import Callable

from constants import CHUNK_OVERLAP, CHUNK_SIZE, MD_SINGLE_CHUNK_MAX

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)
_MD_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})


def chunk_params() -> tuple[int, int]:
    """(chunk_size, overlap) from env or defaults."""
    size = CHUNK_SIZE
    overlap = CHUNK_OVERLAP
    try:
        if os.environ.get("LLMLIBRARIAN_CHUNK_SIZE"):
            size = max(100, min(int(os.environ["LLMLIBRARIAN_CHUNK_SIZE"]), 4000))
        if os.environ.get("LLMLIBRARIAN_CHUNK_OVERLAP"):
            overlap = max(0, min(int(os.environ["LLMLIBRARIAN_CHUNK_OVERLAP"]), size // 2))
    except (TypeError, ValueError):
        pass
    return (size, overlap)


def md_single_chunk_max() -> int:
    """Max chars for indexing a markdown file as one chunk (env-overridable)."""
    try:
        if os.environ.get("LLMLIBRARIAN_MD_SINGLE_CHUNK_MAX"):
            return max(200, min(int(os.environ["LLMLIBRARIAN_MD_SINGLE_CHUNK_MAX"]), 100_000))
    except (TypeError, ValueError):
        pass
    return MD_SINGLE_CHUNK_MAX


def is_markdown_path(source_path: str) -> bool:
    lower = (source_path or "").lower()
    return any(lower.endswith(suf) for suf in _MD_SUFFIXES)


def chunk_text(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[tuple[str, int]]:
    """Returns list of (chunk_text, line_start). line_start is 1-based approximate."""
    if size is None or overlap is None:
        psize, poverlap = chunk_params()
        size = size if size is not None else psize
        overlap = overlap if overlap is not None else poverlap
    if not text.strip():
        return []
    lines = text.split("\n")
    result: list[tuple[str, int]] = []
    current: list[str] = []
    current_len = 0
    line_start = 1
    for i, line in enumerate(lines):
        line_num = i + 1
        if len(line) > size:
            if current:
                result.append(("\n".join(current), line_start))
                current = []
                current_len = 0
            n = len(line)
            start = 0
            while start < n:
                end = min(start + size, n)
                result.append((line[start:end], line_num))
                if end >= n:
                    break
                next_start = end - overlap
                if next_start <= start:
                    next_start = start + 1
                start = next_start
            line_start = line_num + 1
            continue
        current.append(line)
        current_len += len(line) + 1
        if current_len >= size:
            chunk = "\n".join(current)
            result.append((chunk, line_start))
            overlap_len = 0
            overlap_lines: list[str] = []
            for j in range(len(current) - 1, -1, -1):
                overlap_lines.insert(0, current[j])
                overlap_len += len(current[j]) + 1
                if overlap_len >= overlap and j > 0:
                    line_start = line_num - len(overlap_lines) + 1
                    break
            current = overlap_lines
            current_len = overlap_len
            if not current:
                line_start = line_num + 1
    if current:
        result.append(("\n".join(current), line_start))
    return result


def _split_on_headings(text: str) -> list[tuple[str, int]]:
    """Split markdown into (section_text, 1-based line_start) on ATX headings."""
    if not text.strip():
        return []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(text, 1)]

    sections: list[tuple[str, int]] = []
    # Preamble before the first heading
    first = matches[0]
    if first.start() > 0:
        preamble = text[: first.start()]
        if preamble.strip():
            sections.append((preamble, 1))

    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        section = text[start:end]
        if not section.strip():
            continue
        line_start = text.count("\n", 0, start) + 1
        sections.append((section, line_start))
    return sections or [(text, 1)]


def chunk_markdown(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
    single_chunk_max: int | None = None,
    size_chunker: Callable[..., list[tuple[str, int]]] | None = None,
) -> list[tuple[str, int]]:
    """Chunk markdown: one piece when small; else heading sections, then size.

    ``single_chunk_max`` defaults to ``MD_SINGLE_CHUNK_MAX`` / env override.
    Non-markdown callers should keep using ``chunk_text``.
    """
    if size is None or overlap is None:
        psize, poverlap = chunk_params()
        size = size if size is not None else psize
        overlap = overlap if overlap is not None else poverlap
    limit = md_single_chunk_max() if single_chunk_max is None else single_chunk_max
    splitter = size_chunker or chunk_text

    if not text.strip():
        return []
    if len(text) <= limit:
        return [(text, 1)]

    out: list[tuple[str, int]] = []
    for section, line_start in _split_on_headings(text):
        if len(section) <= size:
            out.append((section, line_start))
            continue
        # Size-split oversized sections; remap line_start relative to section.
        for piece, rel_line in splitter(section, size=size, overlap=overlap):
            out.append((piece, line_start + max(0, rel_line - 1)))
    return out
