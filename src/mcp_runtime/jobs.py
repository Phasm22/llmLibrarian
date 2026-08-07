"""The MCP server's Chroma mutex, lock-contention shaping, and ingest runner.

Three things have to line up on every background write, and getting any of them
wrong corrupts the index rather than raising:

1. ``_chroma_lock`` serializes Chroma use across concurrent MCP tool calls.
   ChromaDB's Rust HNSW writer is not safe for concurrent use; simultaneous
   reads and a background reindex once grew link_lists.bin to 680 GB. Reads skip
   it in server mode (see ``read_lock_disabled``); writes never do.
2. The cached singleton client must be released before ingest opens its own
   ``writer_client``, since two live PersistentClients on one persist dir
   corrupt the segment writer.
3. The lock is acquired late -- ingest calls ``pre_write_hook`` once it is ready
   to write -- so scanning and chunking, the slow parts, do not block readers.

``start_ingest_job`` is the single implementation of that sequence. Both
``add_silo`` and ``trigger_reindex`` go through it; when it was inlined per tool
the two copies drifted.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from chroma_lock import _lock_timeout_seconds

_LOGGER_NAME = "llmLibrarian.mcp"

# Serializes ChromaDB client use across concurrent MCP tool calls (in-process).
# Cross-process safety uses flock in src/chroma_lock.py.
_chroma_lock = threading.Lock()

# Serializes background ingest jobs against each other.
_ingest_lock = threading.Lock()

_outcome_lock = threading.Lock()
_last_outcome: dict[str, dict] = {}
_active_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Lock budget
# ---------------------------------------------------------------------------


def mcp_lock_timeout_seconds() -> float | None:
    """Wait budget for the in-process mutex; None means block indefinitely.

    Delegates to chroma_lock so LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS=0 means
    the same thing here as it does for the flock layer.
    """
    return _lock_timeout_seconds(env_name="LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS")


def acquire_chroma_lock(operation: str) -> None:
    """Take the in-process Chroma mutex, honouring the block-forever sentinel."""
    timeout = mcp_lock_timeout_seconds()
    acquired = (
        _chroma_lock.acquire() if timeout is None else _chroma_lock.acquire(timeout=timeout)
    )
    if not acquired:
        raise TimeoutError(
            f"Timed out after {timeout:g}s waiting for MCP Chroma lock during {operation}. "
            "A background reindex or another tool call is still using Chroma; call health() "
            "for last_background_reindex, then retry or restart the stuck MCP server."
        )


def retry_after_seconds() -> int:
    """Client retry hint: half the lock budget, floor 1s.

    With no timeout configured there is no budget to halve, so fall back to the
    default read wait rather than reporting a nonsense delay.
    """
    timeout = mcp_lock_timeout_seconds()
    if timeout is None:
        return 5
    return max(1, round(timeout / 2))


def read_lock_disabled() -> bool:
    """True when MCP read tools should not take the in-process Chroma mutex.

    The 680 GB corruption this mutex was added for came from an *embedded*
    client: two threads driving one PersistentClient into the Rust HNSW writer
    at once. Under ``chroma run`` no thread here touches HNSW — every call is an
    HTTP request the server orders itself. The mutex then only makes MCP reads
    queue behind this process's own background reindex, which is why a query
    returns ``busy`` for the whole duration of a watcher-triggered index.

    Writes keep taking it in both modes: they are rare, and serializing our own
    ingest against itself is cheap insurance.

    Escape hatch: ``LLMLIBRARIAN_MCP_READ_LOCK=1`` restores the old behavior.
    """
    if (os.environ.get("LLMLIBRARIAN_MCP_READ_LOCK", "").strip().lower()
            in {"1", "true", "yes", "force"}):
        return False
    try:
        from chroma_client import is_http_mode

        return is_http_mode()
    except Exception:
        return False


@contextmanager
def mcp_chroma_lock(operation: str, write: bool = False):
    """Serialize one Chroma-touching tool call against the others."""
    if not write and read_lock_disabled():
        yield
        return
    acquire_chroma_lock(operation)
    try:
        yield
    finally:
        _chroma_lock.release()


# ---------------------------------------------------------------------------
# Contention shaping
# ---------------------------------------------------------------------------


def is_lock_timeout(exc: BaseException) -> bool:
    """True when an exception is a transient Chroma/MCP lock timeout.

    Covers both the cross-process flock (``ChromaLockTimeoutError``, a
    ``TimeoutError`` subclass) and the in-process mutex. These mean "busy,
    retry", not "index broken".
    """
    return isinstance(exc, TimeoutError)


def busy_error(exc: BaseException, operation: str, db_path: str) -> dict:
    """Soft, retryable payload for lock contention.

    Distinct from a hard error so the caller retries instead of treating the DB
    as down or the index as empty.
    """
    return {
        "db_path": db_path,
        "busy": True,
        "retryable": True,
        "retry_after_seconds": retry_after_seconds(),
        "error": (
            f"ChromaDB is busy ({type(exc).__name__} during {operation}); another "
            "index/query is holding the lock. This is transient — retry shortly. "
            "If it persists, call health() / mcp_runtime_status to find the holder."
        ),
    }


def release_chroma() -> None:
    """Drop the cached singleton client. Never raises."""
    try:
        from chroma_client import release

        release()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------


def mark_started(key: str, *, kind: str, path: str, silo: str | None = None) -> None:
    with _outcome_lock:
        _active_jobs[key] = {
            "kind": kind,
            "silo": silo or key,
            "path": path,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }


def mark_finished(key: str, outcome: dict) -> None:
    with _outcome_lock:
        _active_jobs.pop(key, None)
        _last_outcome[key] = outcome


def snapshot() -> dict[str, dict]:
    """Current job state for health()/mcp_runtime_status()."""
    with _outcome_lock:
        return {
            "active_background_jobs": dict(_active_jobs),
            "last_background_reindex": dict(_last_outcome),
        }


def reset_for_tests() -> None:
    with _outcome_lock:
        _active_jobs.clear()
        _last_outcome.clear()


# ---------------------------------------------------------------------------
# The background ingest runner
# ---------------------------------------------------------------------------


def _run_ingest_job(key: str, kind: str, request: Any, silo: str | None) -> None:
    import logging

    logger = logging.getLogger(_LOGGER_NAME)
    path = str(request.path)

    err: str | None = None
    files_ok = 0
    n_failures = 0
    final_slug: str | None = None
    write_lock_held = False

    mark_started(key, kind=kind, path=path, silo=silo)
    try:
        def acquire_for_write() -> None:
            nonlocal write_lock_held
            acquire_chroma_lock(kind)
            write_lock_held = True

        with _ingest_lock:
            from orchestration.ingest import run_ingest

            # Drop the cached singleton before ingest opens its writer_client.
            release_chroma()
            request.pre_write_hook = acquire_for_write
            result = run_ingest(request)
            files_ok = result.files_indexed
            n_failures = result.failures
            # run_ingest already resolves the slug from the registered path.
            final_slug = result.silo_slug
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("%s failed key=%s path=%s", kind, key, path)
        traceback.print_exc(file=sys.stderr)
    finally:
        if write_lock_held:
            _chroma_lock.release()
        mark_finished(key, {
            "silo": final_slug or silo or key,
            "path": path,
            "kind": kind,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "ok": err is None,
            "files_indexed": files_ok,
            "failures": n_failures,
            **({"error": err} if err else {}),
        })
        release_chroma()


# Patch point for tests that need the job to run without a real thread.
_thread_cls = threading.Thread


def start_ingest_job(
    *,
    key: str,
    kind: str,
    request: Any,
    silo: str | None = None,
    thread_factory: Callable[..., Any] | None = None,
) -> str:
    """Run ``request`` on a background thread; return the job's registry key.

    The key is what a caller passes back to ``health()`` to find this job's
    outcome under ``last_background_reindex``, so tools must return it.
    """
    factory = thread_factory or _thread_cls
    t = factory(target=_run_ingest_job, args=(key, kind, request, silo), daemon=True)
    t.start()
    return key
