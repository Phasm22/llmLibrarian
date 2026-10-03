"""read_document: an indexed file back out of the index, in order, paged."""
from __future__ import annotations

import pytest

from document_read import read_indexed_document, trim_overlap


class _Collection:
    def __init__(self, rows: list[tuple[str, dict]]):
        self.rows = rows
        self.wheres: list[dict] = []

    def get(self, *, where, include, limit, offset):
        self.wheres.append(where)
        page = self.rows[offset : offset + limit]
        return {"documents": [d for d, _m in page], "metadatas": [m for _d, m in page]}


def test_trim_overlap_removes_repeated_seam():
    seam = "gamma delta epsilon and the overlap"
    assert trim_overlap("alpha beta " + seam, seam + " zeta") == " zeta"
    # Under 20 characters a match is likely coincidence, so it is kept.
    assert trim_overlap("ends with the", "the start") == "the start"
    assert trim_overlap("no shared text here", "completely different") == "completely different"


def test_chunks_come_back_in_document_order_with_seams_trimmed():
    rows = [
        ("third part of the file, final words.", {"chunk_index": 2}),
        ("first part of the file, opening words and more", {"chunk_index": 0}),
        ("opening words and more then the second part", {"chunk_index": 1}),
    ]
    coll = _Collection(rows)
    out = read_indexed_document(coll, silo="s", source="/f.txt")

    assert coll.wheres[0] == {"$and": [{"silo": "s"}, {"source": "/f.txt"}]}
    assert out["text"].startswith("first part of the file, opening words and more then the second part")
    assert out["text"].count("opening words and more") == 1
    assert out["total_chunks"] == 3 and "next_start_chunk" not in out


def test_paging_resumes_where_the_budget_ran_out():
    rows = [(f"chunk {i} " + "x" * 300, {"chunk_index": i}) for i in range(10)]
    first = read_indexed_document(_Collection(rows), silo="s", source="/f", max_chars=700)
    assert first["next_start_chunk"] == 2

    second = read_indexed_document(_Collection(rows), silo="s", source="/f", start_chunk=2, max_chars=700)
    assert second["text"].startswith("chunk 2 ")
    assert second["start_chunk"] == 2


def test_pdf_pages_are_reported():
    rows = [("p1", {"page": 1, "chunk_index": 0}), ("p2", {"page": 2, "chunk_index": 1})]
    out = read_indexed_document(_Collection(rows), silo="s", source="/f.pdf")
    assert out["pages"] == [1, 2]


def test_unknown_file_points_at_find_files(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    out = mcp_server.read_document("/nowhere/README.md")
    assert out["error"] and out["recommended_action"]["tool"] == "find_files"
