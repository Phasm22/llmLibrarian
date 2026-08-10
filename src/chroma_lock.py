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

**Both locks are skipped in HTTP server mode.** The flock exists to stop two embedded
``PersistentClient`` processes from mutating HNSW on one path at once. When
``chroma run`` fronts the directory, no llmLibrarian process touches disk at all —
the server is the single on-disk reader/writer and orders access itself. Keeping the
flock there serializes our own clients against each other for no safety benefit, which
is how a background index starves a query (shared) or fails a peer ``llmli add``
outright (exclusive).

Overrides, if a stray embedded writer is ever suspected:
``LLMLIBRARIAN_CHROMA_SHARED_LOCK=1`` / ``LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK=1``.
In embedded mode both locks are always taken — there they are load-bearing.

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
# Writers wait far longer than readers: a reader that blocks 5s looks hung to the
# caller, but an `llmli add` queued behind another index has nothing better to do
# than wait, and failing it outright just pushes the retry onto a human.
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_DEFAULT_WRITE_LOCK_TIMEOUT_SECONDS = 120.0
_LOCK_POLL_MIN_SECONDS = 0.02
_LOCK_POLL_MAX_SECONDS = 0.5
_LOCK_POLL_BACKOFF = 1.6
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


def _http_mode() -> bool:
    """True when this process talks to ``chroma run`` over HTTP rather than on disk."""
    try:
        from chroma_client import is_http_mode
    except Exception:
        return False
    try:
        return is_http_mode()
    except Exception:
        return False


def _forced(var: str) -> bool:
    return os.environ.get(var, "").strip().lower() in {"1", "true", "yes", "force"}


def _shared_read_lock_disabled() -> bool:
    """True when the cross-process shared (read) flock should be skipped.

    In HTTP server mode a single ``chroma run`` process is the only on-disk
    reader/writer, and it serializes concurrent access safely. Holding the
    shared flock around reads there is redundant and, worse, blocks all reads
    for the full duration of any writer's exclusive lock — the main way lock
    contention interferes with query availability. Skipping it lets reads flow
    concurrently with an in-progress index write.

    Escape hatch: set ``LLMLIBRARIAN_CHROMA_SHARED_LOCK=1`` (or force) to keep
    the shared flock even in HTTP mode.
    """
    if _forced("LLMLIBRARIAN_CHROMA_SHARED_LOCK"):
        return False
    return _http_mode()


def _exclusive_lock_disabled() -> bool:
    """True when the cross-process exclusive (write) flock should be skipped.

    Same reasoning as the shared lock, one step further. The flock exists to stop
    two *embedded* ``PersistentClient`` processes from mutating HNSW on one path
    concurrently. In HTTP server mode nobody does that: ``chroma run`` is the only
    process touching disk, and it serializes writes itself. The flock then only
    serializes llmLibrarian's own writers against each other — so a `llmli add`
    fails after ``LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS`` waiting on a peer
    whose write the server was already going to order safely.

    Kept as the default in embedded mode, where it is load-bearing.

    Escape hatch: ``LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK=1`` forces the flock even
    in HTTP mode (belt-and-braces if a stray embedded writer is suspected).
    """
    if _forced("LLMLIBRARIAN_CHROMA_EXCLUSIVE_LOCK"):
        return False
    return _http_mode()


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


_BLOCK_FOREVER = {"0", "none", "off", "false", "no"}


def _parse_timeout(raw: str, default: float) -> float | None:
    """Parse one timeout value. Empty means "not set" (caller falls through)."""
    if raw.lower() in _BLOCK_FOREVER:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _lock_timeout_seconds(write: bool = False, env_name: str | None = None) -> float | None:
    """Seconds to wait for a Chroma lock; None means block indefinitely.

    Consulted in order: ``env_name`` (a caller-specific override, e.g. the MCP
    server's in-process mutex), then the writer-only variable, then the shared
    one. Writers get their own much longer default — see the module constants.

    Every lock layer must read its budget through this function. The
    block-forever sentinel is why: a caller that reimplemented it as a plain
    float turns ``...LOCK_TIMEOUT_SECONDS=0`` into an instant timeout in one
    layer while it means "wait forever" in another.
    """
    default = _DEFAULT_WRITE_LOCK_TIMEOUT_SECONDS if write else _DEFAULT_LOCK_TIMEOUT_SECONDS

    candidates: list[str] = []
    if env_name:
        candidates.append(env_name)
    if write:
        candidates.append("LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS")
    candidates.append("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS")

    for name in candidates:
        raw = os.environ.get(name, "").strip()
        if raw:
            return _parse_timeout(raw, default)
    return default


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


def _acquire_flock(
    f: Any, operation: int, *, mode: str, db_path: str, path: Path, write: bool = False
) -> None:
    """Take the flock, waiting up to the configured timeout.

    Polls with exponential backoff rather than a fixed 100ms tick: under
    contention a tight poll makes every waiter wake, fail, and re-queue in
    lockstep, which is how a busy index turns into a thundering herd. Backoff
    keeps the fast path (lock free within a few ms) fast while thinning out
    retries as the wait gets long.
    """
    timeout = _lock_timeout_seconds(write=write)
    if timeout is None:
        fcntl.flock(f.fileno(), operation)
        return

    deadline = time.monotonic() + timeout
    delay = _LOCK_POLL_MIN_SECONDS
    while True:
        try:
            fcntl.flock(f.fileno(), operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                holders = _lock_holders(path)
                holder_text = f" holder_pids={holders}" if holders else ""
                raise ChromaLockTimeoutError(
                    f"Timed out after {timeout:g}s waiting for {mode} ChromaDB lock "
                    f"at {path} (db={db_path}).{holder_text} "
                    "Another llmLibrarian index/query process is using the database; "
                    "retry when it finishes or stop the stuck process."
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * _LOCK_POLL_BACKOFF, _LOCK_POLL_MAX_SECONDS)


@contextmanager
def chroma_shared_lock(db_path: str | Path) -> Iterator[None]:
    """Advisory shared lock for Chroma reads (query/get).

    No-op in HTTP server mode (the ``chroma run`` server is the single on-disk
    reader/writer and serializes safely); see ``_shared_read_lock_disabled``.
    """
    if fcntl is None:
        _warn_no_fcntl_once()
        yield
        return
    if _shared_read_lock_disabled():
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
    """Advisory exclusive lock for Chroma writes (add/delete/repair). Reentrant.

    No-op in HTTP server mode — ``chroma run`` owns the persist directory and
    orders writes itself; see ``_exclusive_lock_disabled``.
    """
    if fcntl is None:
        _warn_no_fcntl_once()
        yield
        return
    if _exclusive_lock_disabled():
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
                _acquire_flock(
                    gate.fd, fcntl.LOCK_EX, mode="exclusive", db_path=key, path=path, write=True
                )
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
