"""Write-in-progress visibility.

A full rebuild deletes a silo's chunks before writing the replacements. A query
landing in that window gets zero chunks and no error, which a model reads as
"the source doesn't say that." These tests pin the signal that tells the two
apart, and the liveness rule that stops a crashed writer pinning a silo in a
permanent "writing" state.
"""

from __future__ import annotations

import json
import os

import pytest

import ingest_journal as ij


def _marker(db, slug, *, kind="full", pid=None):
    ij.write_pending(str(db), slug, kind=kind)
    if pid is not None:
        path = ij._pending_path(str(db), slug)
        data = json.loads(path.read_text())
        data["pid"] = pid
        path.write_text(json.dumps(data))


DEAD_PID = 999_999_999


def test_marker_records_kind_and_pid(tmp_path):
    ij.write_pending(str(tmp_path), "silo-a", kind="full")
    data = json.loads(ij._pending_path(str(tmp_path), "silo-a").read_text())
    assert data["kind"] == "full"
    assert data["pid"] == os.getpid()
    assert data["started_at"]


def test_active_writes_sees_live_writer(tmp_path):
    _marker(tmp_path, "silo-a")
    active = ij.active_writes(str(tmp_path))
    assert "silo-a" in active
    assert active["silo-a"]["kind"] == "full"


def test_active_writes_ignores_dead_writer(tmp_path):
    """A crashed ingest leaves its marker behind; reporting it as an active write
    would pin the silo as 'rebuilding' forever."""
    _marker(tmp_path, "silo-a", pid=DEAD_PID)
    assert ij.active_writes(str(tmp_path)) == {}


def test_check_pending_still_sees_dead_writer(tmp_path):
    """Crash recovery must still fire for an interrupted ingest — that is what the
    marker was originally for."""
    _marker(tmp_path, "silo-a", pid=DEAD_PID)
    assert ij.check_pending(str(tmp_path)) == ["silo-a"]


def test_clear_pending_removes_marker(tmp_path):
    _marker(tmp_path, "silo-a")
    ij.clear_pending(str(tmp_path), "silo-a")
    assert ij.active_writes(str(tmp_path)) == {}
    assert ij.check_pending(str(tmp_path)) == []


def test_write_in_progress_scopes_to_requested_silo(tmp_path):
    _marker(tmp_path, "silo-a")
    assert ij.write_in_progress(str(tmp_path), "silo-a")["rebuilding"] == ["silo-a"]
    assert ij.write_in_progress(str(tmp_path), "silo-b") is None


def test_unscoped_read_sees_any_live_write(tmp_path):
    """An unscoped query spans every silo, so any live write is relevant to it."""
    _marker(tmp_path, "silo-a")
    state = ij.write_in_progress(str(tmp_path))
    assert state["silos"] == ["silo-a"]


def test_incremental_write_is_visible_but_not_incomplete(tmp_path):
    """Incremental adds are additive — readers see stale-but-valid data, so the
    write is reported without the 'may be incomplete' alarm."""
    _marker(tmp_path, "silo-a", kind="incremental")
    state = ij.write_in_progress(str(tmp_path), "silo-a")
    assert state["silos"] == ["silo-a"]
    assert state["rebuilding"] == []
    assert state["results_may_be_incomplete"] is False


def test_no_markers_means_no_signal(tmp_path):
    assert ij.write_in_progress(str(tmp_path)) is None
    assert ij.merge_write_states([None, None]) is None


def test_merge_unions_silos_and_keeps_earliest_start(tmp_path):
    a = {"silos": ["x"], "rebuilding": ["x"], "started_at": "2026-01-02T00:00:00+00:00"}
    b = {"silos": ["y"], "rebuilding": [], "started_at": "2026-01-01T00:00:00+00:00"}
    merged = ij.merge_write_states([a, b])
    assert merged["silos"] == ["x", "y"]
    assert merged["rebuilding"] == ["x"]
    assert merged["results_may_be_incomplete"] is True
    assert merged["started_at"] == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Read-path annotation
# ---------------------------------------------------------------------------


def test_annotate_flags_rebuild_and_marks_retryable():
    from query.core import annotate_write_state

    state = {"silos": ["s"], "rebuilding": ["s"], "results_may_be_incomplete": True}
    result = annotate_write_state({"chunks": [], "coverage_note": "no chunks"}, state, None)

    assert result["write_in_progress"]["rebuilding"] == ["s"]
    assert result["retryable"] is True
    # The note must say the empty result is not evidence of absence.
    assert "not" in result["coverage_note"] and "absence" in result["coverage_note"]
    assert "no chunks" in result["coverage_note"], "existing note should be preserved"


def test_annotate_catches_write_starting_mid_read():
    """Sampled either side of retrieval — a write that begins during the read is
    the case that silently corrupts an answer."""
    from query.core import annotate_write_state

    after = {"silos": ["s"], "rebuilding": ["s"], "results_may_be_incomplete": True}
    result = annotate_write_state({"chunks": []}, None, after)
    assert result["write_in_progress"]["rebuilding"] == ["s"]


def test_annotate_incremental_does_not_mark_retryable():
    from query.core import annotate_write_state

    state = {"silos": ["s"], "rebuilding": [], "results_may_be_incomplete": False}
    result = annotate_write_state({"chunks": [{"text": "x"}]}, state, None)
    assert result["write_in_progress"]["silos"] == ["s"]
    assert "retryable" not in result


def test_annotate_is_a_noop_when_nothing_is_writing():
    from query.core import annotate_write_state

    result = annotate_write_state({"chunks": []}, None, None)
    assert result == {"chunks": []}


# ---------------------------------------------------------------------------
# MCP in-process mutex
# ---------------------------------------------------------------------------


def test_mcp_reads_do_not_queue_behind_a_background_write_in_http_mode(monkeypatch):
    """A watcher-triggered reindex holds the in-process mutex for its whole write
    phase. In HTTP mode reads must not wait on it, or every query returns `busy`
    for the duration of a background index."""
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMLIBRARIAN_MCP_READ_LOCK", raising=False)
    monkeypatch.setattr(mcp_server, "_mcp_lock_timeout_seconds", lambda: 0.01)

    assert mcp_server._chroma_lock.acquire(timeout=1)
    try:
        with mcp_server._mcp_chroma_lock("query_personal_knowledge"):
            pass  # read proceeds while the "background write" holds the mutex
    finally:
        mcp_server._chroma_lock.release()


def test_mcp_writes_still_serialize_in_http_mode(monkeypatch):
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setattr(mcp_server, "_mcp_lock_timeout_seconds", lambda: 0.01)

    assert mcp_server._chroma_lock.acquire(timeout=1)
    try:
        with pytest.raises(TimeoutError):
            with mcp_server._mcp_chroma_lock("repair_silo", write=True):
                pass
    finally:
        mcp_server._chroma_lock.release()


def test_mcp_reads_still_serialize_in_embedded_mode(monkeypatch):
    """Embedded mode is where concurrent HNSW access actually corrupts — the
    mutex must not be skipped there."""
    import mcp_server

    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setattr(mcp_server, "_mcp_lock_timeout_seconds", lambda: 0.01)

    assert mcp_server._chroma_lock.acquire(timeout=1)
    try:
        with pytest.raises(TimeoutError):
            with mcp_server._mcp_chroma_lock("query_personal_knowledge"):
                pass
    finally:
        mcp_server._chroma_lock.release()


def test_mcp_read_lock_override_restores_serialization(monkeypatch):
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_READ_LOCK", "1")
    monkeypatch.setattr(mcp_server, "_mcp_lock_timeout_seconds", lambda: 0.01)

    assert mcp_server._chroma_lock.acquire(timeout=1)
    try:
        with pytest.raises(TimeoutError):
            with mcp_server._mcp_chroma_lock("query_personal_knowledge"):
                pass
    finally:
        mcp_server._chroma_lock.release()


# ---------------------------------------------------------------------------
# Post-write quiesce
# ---------------------------------------------------------------------------


class _FakeCollection:
    """Collection whose vector query fails with the HNSW build-lag error N times."""

    def __init__(self, fail_times=0, error=None):
        self.fail_times = fail_times
        self.error = error or Exception("Error executing plan: Internal error: Error finding id")
        self.queries = 0

    def get(self, **_kw):
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    def query(self, **_kw):
        self.queries += 1
        if self.queries <= self.fail_times:
            raise self.error
        return {"ids": [[]]}


def test_waits_out_index_build_lag(monkeypatch):
    from ingest import _wait_until_queryable

    monkeypatch.setattr("ingest.time.sleep", lambda _s: None)
    coll = _FakeCollection(fail_times=3)
    assert _wait_until_queryable(coll, "silo-a") is True
    assert coll.queries == 4


def test_returns_immediately_on_a_non_index_error(monkeypatch):
    """A broken collection will not fix itself by sleeping — an `llmli add` must
    not pay the full timeout at the end of every run."""
    from ingest import _wait_until_queryable

    slept: list[float] = []
    monkeypatch.setattr("ingest.time.sleep", lambda s: slept.append(s))
    coll = _FakeCollection(fail_times=99, error=ValueError("collection does not exist"))

    assert _wait_until_queryable(coll, "silo-a") is False
    assert coll.queries == 1
    assert slept == []


def test_empty_silo_needs_no_settle():
    from ingest import _wait_until_queryable

    class Empty:
        def get(self, **_kw):
            return {"embeddings": []}

        def query(self, **_kw):  # pragma: no cover - must not be reached
            raise AssertionError("should not query an empty silo")

    assert _wait_until_queryable(Empty(), "silo-a") is True


def test_gives_up_after_the_budget(monkeypatch):
    from ingest import _wait_until_queryable

    monkeypatch.setattr("ingest.time.sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr("ingest.time.monotonic", lambda: next(clock))
    assert _wait_until_queryable(_FakeCollection(fail_times=99), "silo-a") is False


def test_mcp_keeps_the_rebuild_note_when_recomputing_coverage(monkeypatch, tmp_path):
    """query_personal_knowledge recomputes coverage_note from the chunk set; the
    rebuild warning must survive that, since the note is what the model reads."""
    import contextlib
    import sys
    from types import SimpleNamespace

    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(mcp_server, "_mcp_chroma_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setitem(
        sys.modules,
        "query.core",
        SimpleNamespace(
            run_retrieve=lambda **_kw: {
                "chunks": [],
                "write_in_progress": {
                    "silos": ["s"],
                    "rebuilding": ["s"],
                    "results_may_be_incomplete": True,
                },
            }
        ),
    )

    res = mcp_server.query_personal_knowledge("q", silo="s")

    assert "absence of evidence" in res["coverage_note"]
    assert res["write_in_progress"]["rebuilding"] == ["s"]


def test_run_retrieve_surfaces_the_flag(monkeypatch, tmp_path):
    """End-to-end through the read choke point every tool goes through."""
    import query.core as core

    _marker(tmp_path, "silo-a")
    monkeypatch.setattr(core, "resolve_silo_to_slug", lambda *_a, **_k: "silo-a")
    monkeypatch.setattr(core, "route_intent", lambda _q: "LOOKUP")
    monkeypatch.setattr(
        "query.retrieve_locked.execute_retrieve_chroma_phase",
        lambda **_kw: {"chunks": [], "coverage_note": "no chunks returned"},
    )

    result = core.run_retrieve("anything", silo="silo-a", db_path=str(tmp_path))

    assert result["write_in_progress"]["results_may_be_incomplete"] is True
    assert result["retryable"] is True
