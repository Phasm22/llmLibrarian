"""read_document pages are lossless: following the cursor reproduces the indexed text."""
from __future__ import annotations

import random

from document_read import read_indexed_document
from ingest import _chunks_from_content, _chunks_from_pdf_pages


class _Collection:
    def __init__(self, rows: list[tuple[str, dict]]):
        self.rows = rows
        self.calls = 0

    def get(self, *, where, include, limit, offset):
        self.calls += 1
        page = self.rows[offset : offset + limit]
        return {"documents": [d for d, _m in page], "metadatas": [m for _d, m in page]}


def _text_doc(lines: int = 120) -> str:
    rng = random.Random(7)
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
    return "\n".join(
        f"{i:03d} " + " ".join(rng.choice(words) for _ in range(rng.randint(4, 14))) for i in range(lines)
    )


def _rows_from_ingest(text: str, source: str = "/notes/long.txt") -> list[tuple[str, dict]]:
    rows = [(doc, meta) for _cid, doc, meta in _chunks_from_content("f", text, source, 1.0)]
    assert len(rows) > 3 and all("chunk_index" not in m for _d, m in rows)
    random.Random(3).shuffle(rows)
    return rows


def _read_all(rows, max_chars: int) -> tuple[str, int]:
    """Follow next_cursor until the document ends."""
    out, pages = [], 0
    cursor = None
    while True:
        page = read_indexed_document(_Collection(rows), silo="s", source="/x", cursor=cursor, max_chars=max_chars)
        assert "error" not in page
        out.append(page["text"])
        pages += 1
        assert pages < 500, "cursor made no progress"
        if "next_cursor" not in page:
            return "".join(out), pages
        cursor = page["next_cursor"]


def test_whole_read_reproduces_the_ingested_text():
    text = _text_doc()
    page = read_indexed_document(_Collection(_rows_from_ingest(text)), silo="s", source="/x", max_chars=20000)
    assert page["text"] == text


def test_paged_read_reproduces_the_text_with_no_repeated_seams():
    text = _text_doc()
    whole, pages = _read_all(_rows_from_ingest(text), max_chars=1500)
    assert pages > 2
    assert whole == text


def test_chunk_aligned_resume_trims_the_seam_with_the_previous_page():
    text = _text_doc()
    rows = _rows_from_ingest(text)
    first = read_indexed_document(_Collection(rows), silo="s", source="/x", max_chars=1500)
    second = read_indexed_document(
        _Collection(rows), silo="s", source="/x", start_chunk=first["next_start_chunk"], max_chars=1500
    )
    assert text.startswith(first["text"] + second["text"])


def test_oversized_chunk_remainder_is_not_skipped():
    big = "\n".join(f"page two line {i} " + "x" * 80 for i in range(200))  # ~19 KB, one chunk
    pages_in = [("short first page", 1), (big, 2), ("short last page", 3)]
    rows = [(doc, meta) for _cid, doc, meta in _chunks_from_pdf_pages("f", pages_in, "/r.pdf", 1.0)]
    whole, pages = _read_all(rows, max_chars=6000)
    assert pages >= 4
    assert whole == "short first page" + big + "short last page"


def test_oversized_chunk_page_offers_only_the_lossless_cursor():
    big = "y " * 5000
    page = read_indexed_document(_Collection([(big, {"page": 1})]), silo="s", source="/x", max_chars=6000)
    assert page["truncated_chunk"] is True
    assert page["next_cursor"]
    # Following a chunk number here would skip the rest of the chunk or loop on it.
    assert "next_start_chunk" not in page


def test_chunk_boundary_page_keeps_the_legacy_field_too():
    rows = [(f"chunk {i} " + "x" * 300, {"line_start": i * 10 + 1}) for i in range(10)]
    first = read_indexed_document(_Collection(rows), silo="s", source="/f", max_chars=700)
    assert first["next_start_chunk"] == 2 and first["next_cursor"]
    by_chunk = read_indexed_document(_Collection(rows), silo="s", source="/f", start_chunk=2, max_chars=700)
    by_cursor = read_indexed_document(_Collection(rows), silo="s", source="/f", cursor=first["next_cursor"], max_chars=700)
    assert by_chunk["text"] == by_cursor["text"]


def test_cursor_from_a_since_reindexed_document_is_refused():
    rows = [(f"chunk {i} " + "x" * 300, {"line_start": i * 10 + 1, "chunk_hash": f"h{i}"}) for i in range(10)]
    first = read_indexed_document(_Collection(rows), silo="s", source="/f", max_chars=700)
    rows[3] = ("edited " + "x" * 300, {"line_start": 31, "chunk_hash": "edited"})
    page = read_indexed_document(_Collection(rows), silo="s", source="/f", cursor=first["next_cursor"], max_chars=700)
    assert page["cursor_stale"] is True and page["text"] == "" and page["error"]


def test_mcp_stale_cursor_points_back_to_the_start(monkeypatch, tmp_path):
    import contextlib

    import mcp_server

    rows = [(f"chunk {i} " + "x" * 300, {"line_start": i * 10 + 1, "chunk_hash": f"h{i}"}) for i in range(10)]

    class _Client:
        def get_or_create_collection(self, **_k):
            return _Collection(rows)

    monkeypatch.setattr(mcp_server, "_DB_PATH", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_resolve_indexed_file", lambda p, s: ("/f.txt", "s", [], None))
    monkeypatch.setattr(mcp_server, "_mcp_chroma_lock", lambda *_a, **_k: contextlib.nullcontext())
    monkeypatch.setattr("chroma_client.get_client", lambda _db: _Client())
    first = mcp_server.read_document("/f.txt", max_chars=700)
    rows[0] = ("changed " + "x" * 300, {"line_start": 1, "chunk_hash": "changed"})
    out = mcp_server.read_document("/f.txt", max_chars=700, cursor=first["next_cursor"])
    assert out["cursor_stale"] and out["recommended_action"]["tool"] == "read_document"
    assert out["recommended_action"]["args"] == {"path": "/f.txt"}


def test_malformed_cursor_is_an_error_not_a_restart():
    page = read_indexed_document(_Collection([("a", {})]), silo="s", source="/f", cursor="garbage")
    assert page["error"] and page["text"] == ""
