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


def test_exclusive_lock_skipped_in_http_mode(monkeypatch, tmp_path):
    """`chroma run` owns the persist dir and orders writes itself, so the write
    flock only serializes our own clients — an `llmli add` must not fail waiting
    on a peer index."""
    db = str(tmp_path / "db")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("exclusive lock must not call flock in HTTP mode")

    monkeypatch.setattr(cl.fcntl, "flock", boom)
    with cl.chroma_exclusive_lock(db):
        pass


def test_exclusive_lock_http_override_keeps_flock(monkeypatch, tmp_path):
    db = str(tmp_path / "db")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK", "1")

    calls: list[int] = []
    real_flock = cl.fcntl.flock

    def counting_flock(fd, operation):
        calls.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(cl.fcntl, "flock", counting_flock)
    with cl.chroma_exclusive_lock(db):
        pass
    assert calls, "override should still acquire the exclusive flock"


def test_exclusive_lock_still_taken_in_embedded_mode(monkeypatch, tmp_path):
    """Embedded mode is where the flock is load-bearing — it must not regress."""
    db = str(tmp_path / "db")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)

    with cl.chroma_exclusive_lock(db):
        assert cl.chroma_lock_snapshot(db)["holder_pids"]


def test_writers_get_a_longer_budget_than_readers(monkeypatch):
    """A blocked reader looks hung to a caller; a queued writer should just wait."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS", raising=False)

    read_budget = cl._lock_timeout_seconds()
    write_budget = cl._lock_timeout_seconds(write=True)

    assert write_budget > read_budget
    assert write_budget >= 60


def test_write_lock_timeout_env_overrides_writers_only(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS", "7")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", raising=False)

    assert cl._lock_timeout_seconds(write=True) == 7.0
    assert cl._lock_timeout_seconds() == cl._DEFAULT_LOCK_TIMEOUT_SECONDS


def test_generic_timeout_env_still_applies_to_both(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "3")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS", raising=False)

    assert cl._lock_timeout_seconds() == 3.0
    assert cl._lock_timeout_seconds(write=True) == 3.0


def _virtual_clock(monkeypatch):
    """Make sleep advance a fake monotonic clock so the wait loop is deterministic
    and costs no wall-clock time."""
    now = [0.0]
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(cl.time, "sleep", fake_sleep)
    monkeypatch.setattr(cl.time, "monotonic", lambda: now[0])
    return sleeps


def _always_busy(monkeypatch):
    def flock(_fd, operation):
        if operation & cl.fcntl.LOCK_NB:
            raise BlockingIOError()

    monkeypatch.setattr(cl.fcntl, "flock", flock)


def test_acquire_backs_off_instead_of_hot_polling(monkeypatch, tmp_path):
    """Fixed-interval polling makes every waiter retry in lockstep; backoff thins
    retries out as the wait grows."""
    db = str(tmp_path / "db")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "1")
    sleeps = _virtual_clock(monkeypatch)
    _always_busy(monkeypatch)

    with pytest.raises(cl.ChromaLockTimeoutError):
        with cl.chroma_shared_lock(db):
            pass

    assert len(sleeps) > 2
    assert sleeps[0] == cl._LOCK_POLL_MIN_SECONDS
    assert sleeps[-1] > sleeps[0], "delay should grow under sustained contention"
    assert max(sleeps) <= cl._LOCK_POLL_MAX_SECONDS
    # Backoff, not a hot loop: a 1s wait at the 20ms floor would be 50 polls.
    assert len(sleeps) < 20


def test_backoff_never_sleeps_past_the_deadline(monkeypatch, tmp_path):
    """The final sleep is clamped to the remaining budget, so a long max-delay
    can't overshoot a short timeout."""
    db = str(tmp_path / "db")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0.05")
    sleeps = _virtual_clock(monkeypatch)
    _always_busy(monkeypatch)

    with pytest.raises(cl.ChromaLockTimeoutError):
        with cl.chroma_shared_lock(db):
            pass

    assert sum(sleeps) <= 0.05


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


# ── holder introspection: both backends, on every host ──────────────────────
# Linux reads /proc/locks; macOS/BSD has no procfs and its lsof leaves the
# lock field blank, so it falls back to "who has the lock file open". Cover
# both paths everywhere so CI (Linux) still guards the lsof branch.


def test_lock_holders_prefers_procfs_when_present(monkeypatch, tmp_path):
    target = tmp_path / "chroma.lock"
    target.write_text("")
    monkeypatch.setattr(cl.Path, "exists", lambda self: True)
    monkeypatch.setattr(cl, "_lock_holders_proc", lambda p: [101])
    monkeypatch.setattr(
        cl, "_lock_holders_lsof", lambda p: pytest.fail("lsof must not run when procfs exists")
    )

    assert cl._lock_holders(target) == [101]


def test_lock_holders_falls_back_to_lsof_without_procfs(monkeypatch, tmp_path):
    target = tmp_path / "chroma.lock"
    target.write_text("")
    monkeypatch.setattr(cl.Path, "exists", lambda self: False)
    monkeypatch.setattr(cl, "_lock_holders_lsof", lambda p: [202])

    assert cl._lock_holders(target) == [202]


def test_lsof_backend_parses_pids(monkeypatch, tmp_path):
    target = tmp_path / "chroma.lock"
    monkeypatch.setattr(
        cl.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "p310\nf3\np310\np42\n", "returncode": 0})(),
    )

    assert cl._lock_holders_lsof(target) == [42, 310]


def test_lsof_backend_treats_no_holders_as_empty_not_error(monkeypatch, tmp_path):
    """lsof exits 1 when nobody has the file open — that is 'uncontended'."""
    target = tmp_path / "chroma.lock"
    monkeypatch.setattr(
        cl.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "", "returncode": 1})(),
    )

    assert cl._lock_holders_lsof(target) == []


def test_lsof_backend_survives_missing_binary(monkeypatch, tmp_path):
    target = tmp_path / "chroma.lock"

    def _boom(*a, **k):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(cl.subprocess, "run", _boom)

    assert cl._lock_holders_lsof(target) == []


def test_snapshot_flags_whether_holder_pids_are_exact(tmp_path):
    db = str(tmp_path / "db")
    snapshot = cl.chroma_lock_snapshot(db)
    assert snapshot["holder_pids_exact"] is cl.Path("/proc/locks").exists()


def test_procfs_backend_reports_holders_but_not_waiters(monkeypatch, tmp_path):
    """Pins the /proc/locks column layout so Linux stays covered from any host.

    The '->' rows are processes blocked on the lock; they shift every field
    by one and must be skipped rather than misparsed.
    """
    import os as _os
    from pathlib import Path as _Path

    target = tmp_path / "chroma.lock"
    target.write_text("")
    st = target.stat()
    dev = f"{_os.major(st.st_dev):02x}:{_os.minor(st.st_dev):02x}"
    ino = st.st_ino
    sample = (
        "1: POSIX  ADVISORY  WRITE 1234 00:1b:9999 0 EOF\n"
        f"2: FLOCK  ADVISORY  WRITE 4321 {dev}:{ino} 0 EOF\n"
        f"2: -> FLOCK  ADVISORY  WRITE 4322 {dev}:{ino} 0 EOF\n"
        "3: FLOCK  ADVISORY  READ  777 fd:01:12345 0 EOF\n"
    )
    orig = _Path.read_text
    monkeypatch.setattr(
        _Path,
        "read_text",
        lambda self, *a, **k: sample if str(self) == "/proc/locks" else orig(self, *a, **k),
    )

    # 4321 holds it; 4322 is only waiting; the other two are unrelated files.
    assert cl._lock_holders_proc(target) == [4321]
