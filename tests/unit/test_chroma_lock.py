"""chroma_lock — cross-process advisory locks (POSIX) or no-op."""

from __future__ import annotations

import pytest

import chroma_lock as cl


def test_exclusive_lock_reentrant_same_thread(tmp_path):
    db = str(tmp_path / "db")
    with cl.chroma_exclusive_lock(db):
        with cl.chroma_exclusive_lock(db):
            pass


def test_shared_lock_context(tmp_path):
    db = str(tmp_path / "db")
    with cl.chroma_shared_lock(db):
        pass


def test_lock_snapshot_reports_holder(tmp_path):
    db = str(tmp_path / "db")
    with cl.chroma_exclusive_lock(db):
        snapshot = cl.chroma_lock_snapshot(db)
    assert snapshot["exists"] is True
    assert snapshot["holder_pids"]


def test_call_wrapper_helpers_stay_removed():
    """chroma_call_shared/exclusive had no production callers and were removed.

    The context managers are the only supported entry points; a thin fn-wrapper
    variant invites a second locking idiom for no benefit.
    """
    assert not hasattr(cl, "chroma_call_shared")
    assert not hasattr(cl, "chroma_call_exclusive")


def test_shared_lock_timeout_surfaces_busy_db(monkeypatch, tmp_path):
    db = str(tmp_path / "db")

    def fake_flock(_fd, operation):
        if operation & cl.fcntl.LOCK_NB:
            raise BlockingIOError()

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0.001")
    monkeypatch.setattr(cl.fcntl, "flock", fake_flock)

    with pytest.raises(cl.ChromaLockTimeoutError, match="waiting for shared ChromaDB lock"):
        with cl.chroma_shared_lock(db):
            pass
