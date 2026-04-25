"""Coverage for operations._inject_staleness and op_list_silos(check_staleness=True)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import operations as ops
from state import update_silo


def _set_mtime(path, when_iso: str) -> None:
    ts = datetime.fromisoformat(when_iso).timestamp()
    os.utime(path, (ts, ts))


def test_inject_staleness_clean_when_files_predate_index(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.txt").write_text("a")
    _set_mtime(src / "a.txt", "2026-04-01T00:00:00+00:00")

    entry = {"path": str(src), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is False
    assert entry["stale_file_count"] == 0
    assert entry["newest_source_mtime_iso"] is None


def test_inject_staleness_flags_files_modified_after_index(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "old.txt").write_text("old")
    _set_mtime(src / "old.txt", "2026-04-01T00:00:00+00:00")
    (src / "new.txt").write_text("new")
    _set_mtime(src / "new.txt", "2026-04-25T12:00:00+00:00")

    entry = {"path": str(src), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is True
    assert entry["stale_file_count"] == 1
    assert entry["newest_source_mtime_iso"] == "2026-04-25T12:00:00Z"


def test_inject_staleness_uses_strict_greater_than(tmp_path):
    """Files with mtime exactly matching the index timestamp are NOT stale.

    This protects the documented race-noise guidance in AGENTS.md: files
    written at the same wall-clock second as `now_iso` capture should not
    flag as stale just because the comparison is loose.
    """
    src = tmp_path / "docs"
    src.mkdir()
    (src / "edge.txt").write_text("edge")
    indexed_iso = "2026-04-24T05:21:53+00:00"
    _set_mtime(src / "edge.txt", indexed_iso)

    entry = {"path": str(src), "updated": indexed_iso}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is False
    assert entry["stale_file_count"] == 0


def test_inject_staleness_walks_subdirectories(tmp_path):
    src = tmp_path / "docs"
    nested = src / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "buried.txt").write_text("buried")
    _set_mtime(nested / "buried.txt", "2026-04-25T00:00:00+00:00")

    entry = {"path": str(src), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is True
    assert entry["stale_file_count"] == 1


def test_inject_staleness_missing_source_path(tmp_path):
    entry = {"path": str(tmp_path / "does-not-exist"), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is None
    assert entry["staleness_note"] == "source path not accessible"
    assert "stale_file_count" not in entry


def test_inject_staleness_empty_path(tmp_path):
    entry = {"path": "", "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is None
    assert entry["staleness_note"] == "source path not accessible"


def test_inject_staleness_unparseable_timestamp(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.txt").write_text("a")

    entry = {"path": str(src), "updated": "not-an-iso-timestamp"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is None
    assert entry["staleness_note"] == "cannot parse updated timestamp"


def test_inject_staleness_skips_unreadable_files(tmp_path, monkeypatch):
    """getmtime OSError on one file should be skipped, not abort the whole walk."""
    src = tmp_path / "docs"
    src.mkdir()
    (src / "ok.txt").write_text("ok")
    _set_mtime(src / "ok.txt", "2026-04-25T00:00:00+00:00")
    (src / "broken.txt").write_text("broken")

    real_getmtime = os.path.getmtime

    def _mtime_or_raise(path: str) -> float:
        if path.endswith("broken.txt"):
            raise OSError("simulated permission error")
        return real_getmtime(path)

    monkeypatch.setattr(os.path, "getmtime", _mtime_or_raise)

    entry = {"path": str(src), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is True
    assert entry["stale_file_count"] == 1


def test_inject_staleness_walk_error(tmp_path, monkeypatch):
    src = tmp_path / "docs"
    src.mkdir()

    def _boom(_path):
        raise PermissionError("walk denied")

    monkeypatch.setattr(os, "walk", _boom)

    entry = {"path": str(src), "updated": "2026-04-24T00:00:00+00:00"}
    ops._inject_staleness(entry)

    assert entry["is_stale"] is None
    assert entry["staleness_note"].startswith("walk error:")


# ---- op_list_silos(check_staleness=True) end-to-end ------------------------


def test_op_list_silos_db_exists_flag(tmp_path):
    out = ops.op_list_silos(str(tmp_path / "no-such-db"))
    assert out["db_exists"] is False
    assert out["silo_count"] == 0
    assert out["silos"] == []


def test_op_list_silos_check_staleness_wires_inject(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    src = tmp_path / "docs"
    src.mkdir()
    fresh = src / "fresh.txt"
    fresh.write_text("fresh")
    _set_mtime(fresh, "2026-04-25T00:00:00+00:00")

    update_silo(
        db,
        "docs-1234abcd",
        str(src),
        1,
        1,
        "2026-04-24T00:00:00+00:00",
        display_name="Docs",
    )

    out = ops.op_list_silos(str(db), check_staleness=True)

    assert out["db_exists"] is True
    assert out["silo_count"] == 1
    row = out["silos"][0]
    assert row["is_stale"] is True
    assert row["stale_file_count"] == 1
    assert row["newest_source_mtime_iso"] == "2026-04-25T00:00:00Z"


def test_op_list_silos_default_skips_staleness(tmp_path):
    """check_staleness defaults to False — keeps the cheap path cheap."""
    db = tmp_path / "db"
    src = tmp_path / "docs"
    src.mkdir()

    update_silo(
        db,
        "docs-1234abcd",
        str(src),
        0,
        0,
        "2026-04-24T00:00:00+00:00",
    )

    out = ops.op_list_silos(str(db))

    row = out["silos"][0]
    assert "is_stale" not in row
    assert "stale_file_count" not in row
