"""op_db_storage_summary — on-disk Chroma layout without opening the client."""

from __future__ import annotations

import operations as ops
from state import record_index_error, set_last_failures, update_silo


def test_storage_summary_missing_dir(tmp_path):
    r = ops.op_db_storage_summary(str(tmp_path / "missing"))
    assert "error" in r


def test_storage_summary_totals_and_bloat(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_HNSW_BLOAT_BYTES", 100)
    db = tmp_path / "my_brain_db"
    seg = db / "test-uuid"
    seg.mkdir(parents=True)
    (db / "chroma.sqlite3").write_bytes(b"abc")
    (seg / "link_lists.bin").write_bytes(b"x" * 50)
    (seg / "other.bin").write_bytes(b"z")
    r = ops.op_db_storage_summary(str(db))
    assert r["db_path"] == str(db.resolve())
    assert r["db_total_bytes"] == 3 + 50 + 1
    assert len(r["link_lists"]) == 1
    assert r["link_lists"][0]["bytes"] == 50
    assert r["chroma_hnsw_bloat"] is False
    assert r["chroma_hnsw_bloat_note"] is None

    (seg / "link_lists.bin").write_bytes(b"y" * 150)
    r2 = ops.op_db_storage_summary(str(db))
    assert r2["chroma_hnsw_bloat"] is True
    assert r2["chroma_hnsw_bloat_note"]


def test_list_silos_surfaces_query_and_ingest_failures(tmp_path):
    db = tmp_path / "db"
    root = tmp_path / "docs"
    root.mkdir()
    update_silo(db, "docs-1234abcd", str(root), 2, 10, "2026-04-24T00:00:00+00:00", display_name="Docs")
    record_index_error(db, "docs-1234abcd", RuntimeError("chroma boom"))
    set_last_failures(db, [{"path": str(root / "bad.pdf"), "error": "parse failed"}])

    out = ops.op_list_silos(str(db))

    row = out["silos"][0]
    assert row["has_index_errors"] is True
    assert row["last_index_error_time"]
    assert row["has_ingest_failures"] is True
    assert row["last_ingest_failure_count"] == 1
    assert out["last_ingest_failure_count"] == 1
