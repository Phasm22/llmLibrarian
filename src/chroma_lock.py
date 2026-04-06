"""
Cross-process ChromaDB access coordination.

Chroma's on-disk HNSW (link_lists.bin) is not safe under concurrent writers
or unsynchronized read/write from multiple processes. We use flock on a file
inside the persist directory:

- chroma_shared_lock: LOCK_SH per context (separate fd each time; multiple
  concurrent readers in one process each hold SH on their own fd — POSIX OK).
- chroma_exclusive_lock: LOCK_EX with same-process reentrancy (one fd, depth
  counter) so op_repair_silo → run_add does not self-deadlock.

Non-POSIX: locking is a no-op (see chroma_lock_available()).

``run_retrieve`` and ``run_ask`` (CLI / pal) take a shared lock around Chroma access so
reads coordinate with exclusive writers; writers block until shared readers finish.

Policy layers (do not mix up which one you are holding):

+------------------+------------------------------------------+-----------------------------+
| Layer            | Mechanism                                | Scope                       |
+==================+==========================================+=============================+
| Cross-process    | ``chroma_shared_lock`` /                 | All processes hitting the |
|                  | ``chroma_exclusive_lock`` (flock file)   | same persist directory      |
+------------------+------------------------------------------+-----------------------------+
| MCP in-process   | ``threading.Lock`` in ``mcp_server``     | Concurrent MCP tool calls   |
|                  | (``_chroma_lock``) around engine entry   | in one Python process       |
+------------------+------------------------------------------+-----------------------------+
| CLI / pal        | Rely on flock inside engine functions    | Subprocesses coordinate via |
|                  | (``query.core``, ``ingest``,             | persist-dir flock; no extra |
|                  | ``operations``)                          | global CLI mutex            |
+------------------+------------------------------------------+-----------------------------+

MCP tools still invoke code paths that acquire flock, so both layers may apply: the
threading lock serializes overlapping tool calls; flock coordinates with ``llmli`` /
``pal`` subprocesses and other hosts.
"""

from __future__ import annotations

import threading
import warnings
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

_T = TypeVar("_T")

_LOCK_BASENAME = ".llmli_chroma.flock"
_warned_no_fcntl = False


class _ExclusiveGate:
    __slots__ = ("mutex", "depth", "fd")

    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.fd: Any = None


_excl_gates: dict[str, _ExclusiveGate] = defaultdict(_ExclusiveGate)


def chroma_lock_available() -> bool:
    return fcntl is not None


def _resolve_db(db_path: str | Path) -> str:
    return str(Path(db_path).expanduser().resolve())


def _warn_no_fcntl_once() -> None:
    global _warned_no_fcntl
    if not _warned_no_fcntl:
        warnings.warn(
            "fcntl unavailable: Chroma cross-process locking is disabled on this platform. "
            "Avoid running multiple indexers against the same LLMLIBRARIAN_DB.",
            RuntimeWarning,
            stacklevel=3,
        )
        _warned_no_fcntl = True


def _lock_file_path(db_path: str) -> Path:
    root = Path(db_path)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root / _LOCK_BASENAME


@contextmanager
def chroma_shared_lock(db_path: str | Path) -> Iterator[None]:
    """Advisory shared lock for Chroma reads (query/get)."""
    if fcntl is None:
        _warn_no_fcntl_once()
        yield
        return
    key = _resolve_db(db_path)
    path = _lock_file_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


@contextmanager
def chroma_exclusive_lock(db_path: str | Path) -> Iterator[None]:
    """Advisory exclusive lock for Chroma writes (add/delete/repair). Reentrant."""
    if fcntl is None:
        _warn_no_fcntl_once()
        yield
        return
    key = _resolve_db(db_path)
    gate = _excl_gates[key]
    with gate.mutex:
        gate.depth += 1
        if gate.depth == 1:
            path = _lock_file_path(key)
            gate.fd = open(path, "a+", encoding="utf-8")
            fcntl.flock(gate.fd.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        with gate.mutex:
            gate.depth -= 1
            if gate.depth == 0 and gate.fd is not None:
                try:
                    fcntl.flock(gate.fd.fileno(), fcntl.LOCK_UN)
                finally:
                    gate.fd.close()
                    gate.fd = None


def chroma_call_shared(db_path: str | Path, fn: Callable[[], _T]) -> _T:
    with chroma_shared_lock(db_path):
        return fn()


def chroma_call_exclusive(db_path: str | Path, fn: Callable[[], _T]) -> _T:
    with chroma_exclusive_lock(db_path):
        return fn()
