"""E0: a retrieval hit carries a cursor that opens read_document just before it."""
from __future__ import annotations

from document_read import read_indexed_document
from ingest import _chunks_from_content
from query.retrieve_locked import execute_retrieve_chroma_phase

from test_read_document_paging import _Collection


def _rows(source: str = "/notes/a.txt"):
    text = "\n".join(f"{source} line {i:03d} " + "lorem ipsum dolor sit amet " * 2 for i in range(120))
    rows = [(d, m) for _c, d, m in _chunks_from_content("f", text, source, 1.0)]
    return text, sorted(rows, key=lambda r: r[1]["line_start"])


def test_short_page_keeps_the_anchor_and_drops_the_lead_in():
    """The lead-in filled a 1500-char page and the hit never appeared."""
    lead = "One-time install " + "x" * 1400
    hit = "Daily use " + "y" * 1400
    ordered = [
        (lead, {"chunk_hash": "lead", "chunk_index": 0, "line_start": 1}),
        (hit, {"chunk_hash": "hit", "chunk_index": 1, "line_start": 2}),
    ]
    page = read_indexed_document(
        _Collection(ordered), silo="s", source="/x", cursor="a1.hit", max_chars=1500
    )
    assert page["text"].startswith("Daily use")
    assert "One-time install" not in page["text"]
    assert page["anchor_chunk"] == 1 and page["start_chunk"] == 1


def test_read_reports_the_whole_file_length_on_a_short_page():
    text, ordered = _rows()
    page = read_indexed_document(
        _Collection(ordered), silo="s", source="/x", max_chars=1500
    )
    assert page["total_chars"] == len(text)
    assert len(page["text"]) < page["total_chars"]


def test_anchor_cursor_reads_from_one_chunk_before_the_hit():
    _text, ordered = _rows()
    hit_doc, hit_meta = ordered[5]
    page = read_indexed_document(
        _Collection(ordered), silo="s", source="/x", cursor=f"a1.{hit_meta['chunk_hash']}", max_chars=2500
    )
    assert "error" not in page
    assert page["start_chunk"] == 4 and page["anchor_chunk"] == 5
    assert page["text"].startswith(ordered[4][0])
    assert hit_doc[-80:] in page["text"]
    assert page["next_cursor"].startswith("c1.")


def test_anchor_on_the_first_chunk_starts_at_the_top():
    text, ordered = _rows()
    page = read_indexed_document(
        _Collection(ordered), silo="s", source="/x", cursor=f"a1.{ordered[0][1]['chunk_hash']}", max_chars=20000
    )
    assert page["start_chunk"] == 0 and page["text"] == text


def test_anchor_from_another_file_or_version_is_refused():
    _text, ordered = _rows()
    page = read_indexed_document(_Collection(ordered), silo="s", source="/x", cursor="a1.0000000000000000")
    assert page["cursor_stale"] is True and page["text"] == ""


def test_retrieval_chunks_carry_a_read_cursor(monkeypatch):
    class _Client:
        def get_or_create_collection(self, **_k):
            return object()

    meta = {"source": "/a.txt", "silo": "s", "doc_type": "other", "chunk_hash": "abc123"}
    monkeypatch.setattr("query.retrieve_locked._safe_query", lambda *_a, **_k: (["t"], [dict(meta)], [0.1], ["i"], None))
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda _db, _silo: False)
    result = execute_retrieve_chroma_phase(
        db="/tmp/db", intent="LOOKUP", query="t", query_for_retrieval="t", silo_slug="s",
        n_stage1=4, n_results=4, section=None, doc_type=None, db_path="/tmp/db",
        get_chroma_client=lambda _db: _Client(),
    )
    assert result["chunks"][0]["read_cursor"] == "a1.abc123"

    import mcp_contract

    assert mcp_contract.slim_chunks(result["chunks"])[0]["read_cursor"] == "a1.abc123"
