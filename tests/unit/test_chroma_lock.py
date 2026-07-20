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


def test_chroma_call_helpers(tmp_path):
    db = str(tmp_path / "db")

    def f():
        return 42

    assert cl.chroma_call_exclusive(db, f) == 42
    assert cl.chroma_call_shared(db, f) == 42


def test_shared_lock_skipped_in_http_mode(monkeypatch, tmp_path):
    """In HTTP mode the shared read lock is a no-op, so reads never touch flock
    and can't block behind a writer's exclusive lock."""
    db = str(tmp_path / "db")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_SHARED_LOCK", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("shared lock must not call flock in HTTP mode")

    monkeypatch.setattr(cl.fcntl, "flock", boom)
    with cl.chroma_shared_lock(db):
        pass


def test_shared_lock_http_override_keeps_flock(monkeypatch, tmp_path):
    """The escape hatch forces the shared flock even in HTTP mode."""
    db = str(tmp_path / "db")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_SHARED_LOCK", "1")

    calls: list[int] = []
    real_flock = cl.fcntl.flock

    def counting_flock(fd, operation):
        calls.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(cl.fcntl, "flock", counting_flock)
    with cl.chroma_shared_lock(db):
        pass
    assert calls, "override should still acquire the shared flock"


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
