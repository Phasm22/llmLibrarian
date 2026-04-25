"""Coverage for ingest_journal — the write-ahead pending marker.

The journal is the contract that makes crash recovery work: if `run_add`
crashes between `_batch_add` and the silo state write, the next ingest
must observe the leftover marker and force a non-incremental rebuild.
These tests lock in the file-shape and the read/write/check cycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import ingest_journal as ij


def test_write_pending_creates_marker_file(tmp_path):
    db = str(tmp_path / "db")

    ij.write_pending(db, "docs-1234abcd")

    marker = Path(db) / "llmli_pending_docs-1234abcd.json"
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["silo"] == "docs-1234abcd"
    assert "started_at" in payload


def test_clear_pending_removes_marker(tmp_path):
    db = str(tmp_path / "db")
    ij.write_pending(db, "docs-1234abcd")
    assert (Path(db) / "llmli_pending_docs-1234abcd.json").exists()

    ij.clear_pending(db, "docs-1234abcd")

    assert not (Path(db) / "llmli_pending_docs-1234abcd.json").exists()


def test_clear_pending_missing_is_silent(tmp_path):
    db = str(tmp_path / "db")
    Path(db).mkdir()

    ij.clear_pending(db, "never-existed")  # must not raise


def test_check_pending_returns_all_active_slugs(tmp_path):
    db = str(tmp_path / "db")
    ij.write_pending(db, "docs-1111aaaa")
    ij.write_pending(db, "code-2222bbbb")
    ij.write_pending(db, "transcripts-3333cccc")

    pending = ij.check_pending(db)

    assert sorted(pending) == sorted(["docs-1111aaaa", "code-2222bbbb", "transcripts-3333cccc"])


def test_check_pending_on_missing_db_returns_empty_list(tmp_path):
    """Brand-new install: db dir not created yet — check must not raise."""
    pending = ij.check_pending(str(tmp_path / "no-such-db"))
    assert pending == []


def test_full_crash_recovery_cycle(tmp_path):
    """write → check sees it → clear → check is empty again."""
    db = str(tmp_path / "db")

    assert ij.check_pending(db) == []
    ij.write_pending(db, "docs-1234abcd")
    assert "docs-1234abcd" in ij.check_pending(db)
    ij.clear_pending(db, "docs-1234abcd")
    assert ij.check_pending(db) == []


def test_check_pending_skips_corrupt_marker(tmp_path):
    """A truncated/corrupted JSON marker should be silently skipped — never abort
    the recovery scan, and never crash the next ingest."""
    db = tmp_path / "db"
    db.mkdir()
    (db / "llmli_pending_corrupt.json").write_text("{not-json")
    ij.write_pending(str(db), "docs-1234abcd")

    pending = ij.check_pending(str(db))

    assert pending == ["docs-1234abcd"]


def test_check_pending_skips_marker_without_silo_field(tmp_path):
    """A marker missing 'silo' is treated as not-actionable, not-fatal."""
    db = tmp_path / "db"
    db.mkdir()
    (db / "llmli_pending_orphan.json").write_text(json.dumps({"started_at": "x"}))
    ij.write_pending(str(db), "real-1111")

    pending = ij.check_pending(str(db))

    assert pending == ["real-1111"]


def test_pending_path_sanitizes_slashes(tmp_path):
    """A slug with path separators must not break out of the db dir."""
    p = ij._pending_path(str(tmp_path / "db"), "weird/slash\\slug")
    assert p.parent == (tmp_path / "db")
    assert "/" not in p.name[len("llmli_pending_"):]
    assert "\\" not in p.name


def test_write_pending_creates_db_dir_if_missing(tmp_path):
    """write_pending must mkdir(parents=True) so callers can journal before
    the db dir exists."""
    db = tmp_path / "fresh"

    ij.write_pending(str(db), "docs-abcd")

    assert db.is_dir()
    assert (db / "llmli_pending_docs-abcd.json").exists()


def test_write_pending_swallows_errors(monkeypatch, tmp_path):
    """The journal must never raise — a failed write_pending only loses
    crash-recovery quality, it must not abort an ingest."""
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    ij.write_pending(str(tmp_path / "db"), "docs-abcd")  # must not raise
