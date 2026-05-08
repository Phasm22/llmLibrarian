"""op_db_storage_summary — on-disk Chroma layout without opening the client."""

from __future__ import annotations

from pathlib import Path

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


def test_chroma_diagnostics_runs_integrity_check(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    # Minimal sqlite database file recognized by sqlite3.
    import sqlite3

    with sqlite3.connect(str(db / "chroma.sqlite3")) as conn:
        conn.execute("create table if not exists t (id integer);")

    result = ops.op_chroma_diagnostics(str(db))
    assert result["status"] == "ok"
    assert result["sqlite_exists"] is True
    assert result["sqlite_integrity_check"] == "ok"
    assert "l1" in result["repair_ladder"]
    assert "l3" in result["repair_ladder"]


def test_rehydrate_registry_dry_run_and_selection(tmp_path):
    db = tmp_path / "db"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    update_silo(db, "alpha-11111111", str(root_a), 1, 2, "2026-05-01T00:00:00+00:00", display_name="Alpha")
    update_silo(db, "beta-22222222", str(root_b), 1, 2, "2026-05-01T00:00:00+00:00", display_name="Beta")

    out = ops.op_rehydrate_registry(str(db), requested_silos=["alpha"], dry_run=True, verbose=False)
    assert out["dry_run"] is True
    assert out["total_targets"] == 1
    assert out["planned"] == 1
    assert out["errors"] == 0
    assert out["results"][0]["slug"] == "alpha-11111111"


def test_rehydrate_registry_executes_full_add(monkeypatch, tmp_path):
    db = tmp_path / "db"
    source = tmp_path / "docs"
    source.mkdir(parents=True)
    update_silo(db, "docs-12345678", str(source), 1, 2, "2026-05-01T00:00:00+00:00", display_name="Docs")

    calls: list[dict] = []

    def _fake_run_add(**kwargs):
        calls.append(kwargs)
        return 3, 0

    monkeypatch.setattr("operations.run_add", _fake_run_add, raising=False)
    # Ensure the local import inside op_rehydrate_registry resolves to our fake.
    import ingest

    monkeypatch.setattr(ingest, "run_add", _fake_run_add)
    out = ops.op_rehydrate_registry(str(db), dry_run=False, verbose=False)
    assert out["completed"] == 1
    assert out["errors"] == 0
    assert len(calls) == 1
    assert calls[0]["incremental"] is False
    assert calls[0]["forced_silo_slug"] == "docs-12345678"
    assert Path(calls[0]["path"]).resolve() == source.resolve()
