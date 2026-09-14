"""
Cross-process locking for llmLibrarian's JSON state files.

The registry (``llmli_registry.json``), the file manifest, and pal's bookmark
registry are all read-modify-write JSON documents shared by many processes: one
watcher per silo, the MCP server, and any number of ``llmli`` / ``pal``
invocations. Writing each file atomically is not enough — two processes can read
the same document, each modify its own copy, and the second write erases the
first one's change. Holding this lock across *both* the read and the write is
what makes a mutation safe.

This deliberately mirrors ``chroma_lock.py`` (flock, exponential backoff, a
reentrancy gate, a no-op fallback where ``fcntl`` is missing) but is a separate
lock on a separate file, for two reasons:

1. ``chroma_exclusive_lock`` is **disabled in HTTP server mode**, because there
   ``chroma run`` owns the persist directory and orders writes itself. That
   reasoning does not extend to these JSON files: they live on local disk and
   are written by our own processes in every transport mode. This lock is always
   taken.
2. The Chroma lock is held for the length of an index write. Piggy-backing
   registry updates on it would make a one-line metadata change wait behind a
   multi-minute ingest.

Use ``registry_transaction(path)`` around any read-modify-write sequence. It is
reentrant within a process, so a locked function may call another one.
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

# Registry mutations are short (read a small JSON file, edit one key, write it).
# A long wait here means something is stuck, not merely busy — but the waiter is
# usually a watcher with nothing better to do, so the budget is generous enough
# to ride out an unlucky pile-up behind a slow filesystem.
_DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_MIN_SECONDS = 0.005
_POLL_MAX_SECONDS = 0.25
_POLL_BACKOFF = 1.6
_TIMEOUT_ENV = "LLMLIBRARIAN_REGISTRY_LOCK_TIMEOUT_SECONDS"
_BLOCK_FOREVER = {"0", "none", "off", "false", "no"}

_warned_no_fcntl = False


class RegistryLockTimeoutError(TimeoutError):
    """Raised when a registry lock cannot be taken within the configured budget."""


class _Gate:
    """Per-path gate: one flock fd, serialized within the process by an RLock.

    flock coordinates *processes*; it does not serialize threads of one process
    reliably, and a second open of the same file would deadlock or silently
    succeed depending on the platform. So threads are serialized by ``mutex``,
    held for the whole critical section, and ``depth`` then safely tracks
    same-thread reentrancy (an RLock re-entered by its owner does not block),
    letting one locked function call another.
    """

    __slots__ = ("mutex", "depth", "handle")

    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle: Any = None


_gates: dict[str, _Gate] = defaultdict(_Gate)
_gates_guard = threading.Lock()


def registry_lock_available() -> bool:
    return fcntl is not None


def _warn_no_fcntl_once() -> None:
    global _warned_no_fcntl
    if not _warned_no_fcntl:
        warnings.warn(
            "fcntl unavailable: llmLibrarian registry locking is disabled on this platform. "
            "Concurrent writers may lose updates to llmli_registry.json.",
            RuntimeWarning,
            stacklevel=3,
        )
        _warned_no_fcntl = True


def lock_path_for(target: str | Path) -> Path:
    """Lock file guarding ``target``.

    Matches the ``<file>.lock`` convention already used for the file manifest, so
    a process on the old code and one on the new code still exclude each other.
    """
    p = Path(target)
    if p.suffix:
        return p.with_suffix(p.suffix + ".lock")
    return p.with_name(p.name + ".lock")


def _timeout_seconds() -> float | None:
    """Wait budget; None means block indefinitely."""
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    if raw.lower() in _BLOCK_FOREVER:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


def _acquire(handle: Any, lock_path: Path) -> None:
    """Take an exclusive flock, polling with backoff up to the timeout.

    Backoff rather than a fixed tick: under contention a tight poll makes every
    waiter wake and re-queue in lockstep, which turns a busy moment into a
    thundering herd.
    """
    timeout = _timeout_seconds()
    if timeout is None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return

    deadline = time.monotonic() + timeout
    delay = _POLL_MIN_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegistryLockTimeoutError(
                    f"Timed out after {timeout:g}s waiting for the registry lock at "
                    f"{lock_path}. Another llmLibrarian process is mid-update; retry "
                    f"once it finishes, or stop the stuck process. Set {_TIMEOUT_ENV} "
                    "to change the budget (0 blocks indefinitely)."
                )
            time.sleep(min(delay, remaining))
            delay = min(delay * _POLL_BACKOFF, _POLL_MAX_SECONDS)


@contextmanager
def registry_transaction(target: str | Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``target`` for read *and* write.

    Wrap the whole read-modify-write sequence, never just the write::

        with registry_transaction(path):
            data = _read(path)
            data["k"] = v
            _write(path, data)

    Reentrant: nesting this for the same path within one process is safe.
    """
    if fcntl is None:
        _warn_no_fcntl_once()
        yield
        return

    lock_path = lock_path_for(target)
    key = str(lock_path.resolve() if lock_path.parent.exists() else lock_path)
    with _gates_guard:
        gate = _gates[key]

    # The RLock is held for the WHOLE critical section, not just while bumping
    # the counter. Releasing it around the yield would let a second thread see
    # depth >= 1, treat itself as a reentrant call, and skip taking the flock —
    # so two threads would be "inside" at once and clobber each other. Holding
    # an RLock throughout means only one thread of this process is ever inside,
    # which is what makes the depth counter a correct same-thread reentrancy
    # check rather than a cross-thread hole.
    gate.mutex.acquire()
    try:
        gate.depth += 1
        if gate.depth == 1:
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                gate.handle = open(lock_path, "a", encoding="utf-8")
                _acquire(gate.handle, lock_path)
            except Exception:
                if gate.handle is not None:
                    gate.handle.close()
                    gate.handle = None
                gate.depth -= 1
                raise
        try:
            yield
        finally:
            gate.depth -= 1
            if gate.depth == 0 and gate.handle is not None:
                try:
                    fcntl.flock(gate.handle.fileno(), fcntl.LOCK_UN)
                finally:
                    gate.handle.close()
                    gate.handle = None
    finally:
        gate.mutex.release()
