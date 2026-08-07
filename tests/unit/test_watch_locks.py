"""Watch-lock parsing is shared between pal and the embedded-write guard.

They must agree on the format. A reader that understands JSON but not the
legacy bare-integer form reports a live watcher as absent, which silently
disarms preflight_embedded_write.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import chroma_client
import watch_locks


def _write_lock(locks_dir, name, payload):
    locks_dir.mkdir(parents=True, exist_ok=True)
    p = locks_dir / f"{name}.pid"
    p.write_text(payload, encoding="utf-8")
    return p


def test_reads_current_json_format(tmp_path):
    p = _write_lock(tmp_path, "notes", json.dumps({"pid": 4242, "silo": "notes"}))
    assert watch_locks.read_watch_lock(p) == {"pid": 4242, "silo": "notes"}
    assert watch_locks.read_watch_lock_pid(p) == 4242


def test_reads_legacy_bare_integer_format(tmp_path):
    p = _write_lock(tmp_path, "legacy", "4242\n")
    assert watch_locks.read_watch_lock(p) == {"pid": 4242}
    assert watch_locks.read_watch_lock_pid(p) == 4242


def test_unreadable_and_empty_locks_are_none(tmp_path):
    assert watch_locks.read_watch_lock(tmp_path / "missing.pid") is None
    assert watch_locks.read_watch_lock(_write_lock(tmp_path, "empty", "  ")) is None
    assert watch_locks.read_watch_lock(_write_lock(tmp_path, "junk", "not-a-pid")) is None


def test_legacy_lock_counts_as_an_active_watcher(tmp_path):
    """The bug: chroma_client's own parser required a dict, so a legacy lock
    made preflight_embedded_write pass while a watcher held the index."""
    locks = tmp_path / "watch_locks"
    _write_lock(locks, "legacy", str(os.getpid()))

    active = watch_locks.active_watchers_for_db(str(tmp_path / "db"), locks)
    assert len(active) == 1
    assert str(os.getpid()) in active[0]


def test_dead_pid_is_not_active(tmp_path):
    locks = tmp_path / "watch_locks"
    _write_lock(locks, "dead", json.dumps({"pid": 2_000_000, "silo": "x"}))
    assert watch_locks.active_watchers_for_db(str(tmp_path / "db"), locks) == []


def test_lock_for_another_db_is_ignored(tmp_path):
    locks = tmp_path / "watch_locks"
    _write_lock(
        locks, "other",
        json.dumps({"pid": os.getpid(), "silo": "x", "db_path": str(tmp_path / "other_db")}),
    )
    assert watch_locks.active_watchers_for_db(str(tmp_path / "db"), locks) == []


def test_lock_matches_db_regardless_of_path_spelling(tmp_path):
    locks = tmp_path / "watch_locks"
    db = tmp_path / "db"
    db.mkdir()
    _write_lock(
        locks, "same",
        json.dumps({"pid": os.getpid(), "silo": "x", "db_path": str(db) + "/"}),
    )
    assert len(watch_locks.active_watchers_for_db(str(db), locks)) == 1


def test_preflight_blocks_on_live_watcher(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setenv("PAL_HOME", str(tmp_path / "pal"))
    _write_lock(tmp_path / "pal" / "watch_locks", "legacy", str(os.getpid()))

    with patch.object(chroma_client, "_mcp_blocks_embedded_write", return_value=None):
        err = chroma_client.preflight_embedded_write(str(tmp_path / "db"))

    assert err is not None
    assert "pal pull --watch" in err
