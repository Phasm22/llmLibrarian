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
import os
import time

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

_T = TypeVar("_T")

_LOCK_BASENAME = ".llmli_chroma.flock"
_DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.1
_warned_no_fcntl = False


class ChromaLockTimeoutError(TimeoutError):
    """Raised when Chroma is busy long enough that blocking would look hung."""


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


def _lock_timeout_seconds() -> float | None:
    raw = os.environ.get("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_LOCK_TIMEOUT_SECONDS
    if raw.lower() in {"0", "none", "off", "false", "no"}:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_LOCK_TIMEOUT_SECONDS


def _lock_holders(path: Path) -> list[int]:
    try:
        stat = path.stat()
        dev = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}"
        inode = str(stat.st_ino)
        lines = Path("/proc/locks").read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    holders: list[int] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        if parts[5].startswith(f"{dev}:{inode}") or (parts[5].endswith(f":{inode}") and dev in parts[5]):
            try:
                holders.append(int(parts[4]))
            except ValueError:
                continue
    return sorted(set(holders))


def chroma_lock_snapshot(db_path: str | Path) -> dict[str, Any]:
    """Return current flock path and holder PIDs without opening Chroma."""
    key = _resolve_db(db_path)
    path = _lock_file_path(key)
    return {
        "path": str(path),
        "exists": path.exists(),
        "available": chroma_lock_available(),
        "holder_pids": _lock_holders(path) if path.exists() else [],
    }


def _acquire_flock(f: Any, operation: int, *, mode: str, db_path: str, path: Path) -> None:
    timeout = _lock_timeout_seconds()
    if timeout is None:
        fcntl.flock(f.fileno(), operation)
        return

    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(f.fileno(), operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                holders = _lock_holders(path)
                holder_text = f" holder_pids={holders}" if holders else ""
                raise ChromaLockTimeoutError(
                    f"Timed out after {timeout:g}s waiting for {mode} ChromaDB lock "
                    f"at {path} (db={db_path}).{holder_text} "
                    "Another llmLibrarian index/query process is using the database; "
                    "retry when it finishes or stop the stuck process."
                )
            time.sleep(_LOCK_POLL_SECONDS)


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
        _acquire_flock(f, fcntl.LOCK_SH, mode="shared", db_path=key, path=path)
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
            try:
                _acquire_flock(gate.fd, fcntl.LOCK_EX, mode="exclusive", db_path=key, path=path)
            except Exception:
                gate.fd.close()
                gate.fd = None
                gate.depth -= 1
                raise
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
