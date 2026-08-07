"""The single background-ingest runner shared by add_silo and trigger_reindex.

This logic used to be copy-pasted per tool. The sequencing it enforces is not
cosmetic: releasing the cached singleton before ingest opens its writer_client,
and releasing the write lock on every exit path, are what keep concurrent MCP
traffic from corrupting the HNSW index.
"""

from __future__ import annotations

import pytest

from mcp_runtime import jobs
from orchestration.ingest import IngestRequest, IngestResult


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_ignored):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _clean_jobs(monkeypatch):
    jobs.reset_for_tests()
    monkeypatch.setattr(jobs, "_thread_cls", _InlineThread)
    yield
    jobs.reset_for_tests()


def _request(tmp_path):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    db = tmp_path / "db"
    db.mkdir(exist_ok=True)
    return IngestRequest(path=str(src), db_path=str(db), incremental=True)


def test_records_outcome_under_the_returned_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "orchestration.ingest.run_ingest",
        lambda req: IngestResult(files_indexed=7, failures=1, silo_slug="resolved-slug"),
    )

    key = jobs.start_ingest_job(key="guessed-key", kind="add_silo", request=_request(tmp_path))

    outcome = jobs.snapshot()["last_background_reindex"][key]
    assert outcome["ok"] is True
    assert outcome["files_indexed"] == 7
    assert outcome["failures"] == 1
    assert outcome["kind"] == "add_silo"
    # The resolved slug is recorded even though the caller guessed the key.
    assert outcome["silo"] == "resolved-slug"


def test_active_job_is_cleared_when_finished(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "orchestration.ingest.run_ingest",
        lambda req: IngestResult(files_indexed=1, failures=0, silo_slug="s"),
    )
    jobs.start_ingest_job(key="k", kind="trigger_reindex", request=_request(tmp_path))
    assert jobs.snapshot()["active_background_jobs"] == {}


def test_failure_is_recorded_not_raised(monkeypatch, tmp_path):
    def boom(req):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr("orchestration.ingest.run_ingest", boom)

    key = jobs.start_ingest_job(key="k", kind="add_silo", request=_request(tmp_path))

    outcome = jobs.snapshot()["last_background_reindex"][key]
    assert outcome["ok"] is False
    assert "ingest exploded" in outcome["error"]
    assert jobs.snapshot()["active_background_jobs"] == {}


def test_write_lock_released_on_the_exception_path(monkeypatch, tmp_path):
    """A leaked write lock wedges every later tool call until restart."""

    def acquire_then_fail(req):
        req.pre_write_hook()
        raise RuntimeError("died mid-write")

    monkeypatch.setattr("orchestration.ingest.run_ingest", acquire_then_fail)

    jobs.start_ingest_job(key="k", kind="add_silo", request=_request(tmp_path))

    assert jobs._chroma_lock.acquire(timeout=0.5), "write lock was leaked"
    jobs._chroma_lock.release()


def test_write_lock_released_on_the_success_path(monkeypatch, tmp_path):
    def acquire_then_succeed(req):
        req.pre_write_hook()
        return IngestResult(files_indexed=1, failures=0, silo_slug="s")

    monkeypatch.setattr("orchestration.ingest.run_ingest", acquire_then_succeed)

    jobs.start_ingest_job(key="k", kind="add_silo", request=_request(tmp_path))

    assert jobs._chroma_lock.acquire(timeout=0.5), "write lock was leaked"
    jobs._chroma_lock.release()


def test_singleton_released_before_ingest_opens_its_writer(monkeypatch, tmp_path):
    """Two live PersistentClients on one persist dir corrupt the segment writer,
    so the cached client must be dropped before run_ingest starts."""
    calls: list[str] = []

    monkeypatch.setattr("chroma_client.release", lambda: calls.append("release"))
    monkeypatch.setattr(
        "orchestration.ingest.run_ingest",
        lambda req: (calls.append("run_ingest"), IngestResult(1, 0, "s"))[1],
    )

    jobs.start_ingest_job(key="k", kind="trigger_reindex", request=_request(tmp_path))

    assert calls.index("release") < calls.index("run_ingest"), calls


def test_pre_write_hook_is_installed_on_the_request(monkeypatch, tmp_path):
    """The lock is taken late, via the hook, so scanning does not block readers."""
    seen: dict = {}

    def capture(req):
        seen["hook"] = req.pre_write_hook
        return IngestResult(1, 0, "s")

    monkeypatch.setattr("orchestration.ingest.run_ingest", capture)
    jobs.start_ingest_job(key="k", kind="add_silo", request=_request(tmp_path))
    assert callable(seen["hook"])


def test_lock_timeout_message_names_the_operation(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "0.01")
    jobs._chroma_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="repair_silo"):
            with jobs.mcp_chroma_lock("repair_silo"):
                pass
    finally:
        jobs._chroma_lock.release()


def test_mcp_lock_timeout_shares_the_flock_sentinel(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    monkeypatch.delenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", raising=False)
    assert jobs.mcp_lock_timeout_seconds() is None
