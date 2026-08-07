"""The MCP server's Chroma mutex and its background ingest runner.

Three things have to line up on every write, and getting any of them wrong
corrupts the index rather than raising:

1. ``_chroma_lock`` serializes *all* Chroma use across concurrent tool calls.
   ChromaDB's Rust HNSW writer is not safe for concurrent use; simultaneous
   reads and a background reindex once grew link_lists.bin to 680 GB.
2. The cached singleton client must be released before ingest opens its own
   ``writer_client``, since two live PersistentClients on one persist dir
   corrupt the segment writer.
3. The lock is acquired late -- ingest calls ``pre_write_hook`` once it is ready
   to write -- so scanning and chunking, which are the slow parts, do not block
   readers.

``start_ingest_job`` is the single implementation of that sequence. Both
``add_silo`` and ``trigger_reindex`` go through it; when this logic was inlined
per tool the two copies drifted.
"""

from __future__ import annotations

import sys
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from chroma_lock import lock_timeout_seconds

_logger_name = "llmLibrarian.mcp"

# Serializes ALL ChromaDB client use across concurrent MCP tool calls (in-process).
# Cross-process safety uses flock in src/chroma_lock.py.
_chroma_lock = threading.Lock()

# Serializes background ingest jobs against each other.
_ingest_lock = threading.Lock()

_outcome_lock = threading.Lock()
_last_outcome: dict[str, dict] = {}
_active_jobs: dict[str, dict] = {}


def mcp_lock_timeout_seconds() -> float | None:
    """Wait budget for the in-process Chroma mutex.

    Shares ``lock_timeout_seconds`` with the flock layer so that setting
    LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS=0 means "block indefinitely" in
    both places instead of block-forever here and fail-instantly there.
    """
    return lock_timeout_seconds("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS")


def _acquire_chroma(operation: str) -> None:
    timeout = mcp_lock_timeout_seconds()
    acquired = _chroma_lock.acquire() if timeout is None else _chroma_lock.acquire(timeout=timeout)
    if not acquired:
        raise TimeoutError(
            f"Timed out after {timeout:g}s waiting for MCP Chroma lock during {operation}. "
            "A background reindex or another tool call is still using Chroma; call health() "
            "for last_background_reindex, then retry or restart the stuck MCP server."
        )


@contextmanager
def mcp_chroma_lock(operation: str):
    """Serialize one Chroma-touching tool call against all others."""
    _acquire_chroma(operation)
    try:
        yield
    finally:
        _chroma_lock.release()


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

    logger = logging.getLogger(_logger_name)
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
            _acquire_chroma(kind)
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
