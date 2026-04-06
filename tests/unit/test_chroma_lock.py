"""chroma_lock — cross-process advisory locks (POSIX) or no-op."""

from __future__ import annotations

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


def test_chroma_call_helpers(tmp_path):
    db = str(tmp_path / "db")

    def f():
        return 42

    assert cl.chroma_call_exclusive(db, f) == 42
    assert cl.chroma_call_shared(db, f) == 42
