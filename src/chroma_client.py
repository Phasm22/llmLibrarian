"""
Singleton ChromaDB client factory.

All modules import get_client() instead of constructing PersistentClient
directly. A single shared client per db_path eliminates the concurrent-write
SIGSEGV caused by multiple Rust HNSW handles on the same files.
"""

import os
import shutil
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import chromadb
from chromadb.config import Settings

_lock = threading.Lock()
_clients: dict[str, "_SafeClient"] = {}
_fallback_warned: set[str] = set()

# Cross-process write-generation tracking.
#
# Background: ChromaDB 1.4+ keeps a process-global Rust/tokio runtime cache.
# Calling clear_system_cache() to flush it crashes (see release() docstring).
# So once a PersistentClient is opened in this process, the on-disk segments
# it cached can be silently invalidated by another process's writer
# (op_repair_silo, run_add via writer_client). The next query then either
# returns garbage or SIGSEGVs in the Rust _query path.
#
# Mitigation: writer_client touches `.llmli_chroma_generation` after each
# successful write. Readers stash the file mtime at client-open time, and
# check_for_writer_changes() reports True when the file has moved. Callers
# that detect this should exit (systemd will restart watchers; the MCP
# wrapper restarts itself via os.execv).
_GEN_FILE_NAME = ".llmli_chroma_generation"
_client_open_generation: dict[str, float] = {}


def _generation_path(db_path: str) -> Path:
    return Path(db_path).expanduser().resolve() / _GEN_FILE_NAME


def _read_generation(db_path: str) -> float:
    p = _generation_path(db_path)
    try:
        return p.stat().st_mtime_ns / 1e9
    except OSError:
        return 0.0


def bump_generation(db_path: str) -> None:
    """Mark this DB as freshly mutated. Call AFTER a successful write commit.

    Idempotent. Safe across processes (uses filesystem mtime). Readers that
    opened their PersistentClient before this call will see
    check_for_writer_changes() == True on their next check.
    """
    p = _generation_path(db_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # `touch` updates mtime; create if missing.
        p.touch(exist_ok=True)
        # Bump mtime explicitly to ensure monotonic forward progress even if
        # two writers finish in the same second on coarse filesystems.
        os.utime(p, None)
    except OSError:
        pass


def check_for_writer_changes(db_path: str) -> bool:
    """Return True if a writer has bumped the generation since this process
    opened the cached PersistentClient for db_path. Returns False if no
    client is cached for this db_path (nothing to invalidate yet)."""
    key = str(Path(db_path).expanduser().resolve())
    with _lock:
        opened_at = _client_open_generation.get(key)
    if opened_at is None:
        return False
    return _read_generation(key) > opened_at


def exit_if_stale(db_path: str, *, exit_code: int = 99) -> None:
    """Sys.exit(exit_code) if check_for_writer_changes(db_path). Designed for
    long-lived reader processes (watcher daemons) under systemd, which will
    restart them automatically. For in-process MCP use, prefer the MCP wrapper
    that re-execs the process."""
    if check_for_writer_changes(db_path):
        print(
            f"[llmli][chroma_client] writer activity detected on {db_path}; "
            f"exiting ({exit_code}) so supervisor restarts with fresh state.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(exit_code)

_HNSW_BLOAT_BYTES = 1 << 30
_MIN_FREE_BYTES = 512 * 1024 * 1024


def _storage_preflight(db_path: str) -> None:
    """Fail before opening Chroma when the persist directory is visibly unsafe."""
    root = Path(db_path).expanduser().resolve()
    usage_root = root if root.exists() else next((p for p in [root.parent, *root.parents] if p.exists()), root)
    try:
        min_free = int(os.environ.get("LLMLIBRARIAN_MIN_FREE_BYTES", _MIN_FREE_BYTES))
    except (TypeError, ValueError):
        min_free = _MIN_FREE_BYTES
    try:
        free = shutil.disk_usage(usage_root).free
    except OSError:
        free = -1
    if free >= 0 and free < min_free:
        raise RuntimeError(
            f"ChromaDB storage preflight failed: only {free} bytes free under {usage_root}. "
            "Free disk space before opening the index."
        )
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        if "link_lists.bin" not in filenames:
            continue
        fp = Path(dirpath) / "link_lists.bin"
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size > _HNSW_BLOAT_BYTES:
            raise RuntimeError(
                f"ChromaDB storage preflight failed: bloated HNSW index {fp} is {size} bytes. "
                "Stop llmLibrarian writers and rebuild my_brain_db before querying or indexing."
            )


class _SafeClient:
    """Thin wrapper that retries get_or_create_collection with DefaultEmbeddingFunction
    when an embedding-function conflict is detected against an existing collection.

    This lets silos created before the mpnet upgrade continue to work without a
    full re-index. New silos still get the caller-supplied (mpnet) function.

    Tracks which EF was actually used per collection so callers can use the same
    one for explicit embedding (avoiding dimension mismatches in parallel ingest).
    """

    def __init__(self, client: chromadb.PersistentClient) -> None:
        self._client = client
        self._effective_efs: dict[str, Any] = {}

    def get_or_create_collection(self, name: str, embedding_function=None, **kwargs):
        try:
            coll = self._client.get_or_create_collection(
                name=name, embedding_function=embedding_function, **kwargs
            )
            self._effective_efs[name] = embedding_function
            return coll
        except Exception as exc:
            msg = str(exc).lower()
            if embedding_function is not None and "conflict" in msg and "default" in msg:
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                fallback_ef = DefaultEmbeddingFunction()
                coll = self._client.get_or_create_collection(
                    name=name, embedding_function=fallback_ef, **kwargs
                )
                self._effective_efs[name] = fallback_ef
                key = f"{id(self._client)}:{name}"
                if key not in _fallback_warned and os.environ.get("LLMLIBRARIAN_QUIET", "").strip().lower() not in {"1", "true", "yes"}:
                    _fallback_warned.add(key)
                    print(
                        "[llmli][WARN] Existing Chroma collection uses the default ONNX embedding "
                        "function; using that for compatibility. Rebuild the DB/collection to switch "
                        "this DB to sentence-transformers/CUDA embeddings.",
                        file=sys.stderr,
                    )
                return coll
            raise

    def get_effective_ef(self, name: str):
        """Return the EF that was actually used when opening the named collection."""
        return self._effective_efs.get(name)

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def get_client(db_path: str) -> "_SafeClient":
    """Return (or create) the shared PersistentClient for db_path.

    If a cached client exists but the write-generation file has advanced
    since it was opened, and LLMLIBRARIAN_EXIT_ON_STALE_GENERATION is set
    (default off in CLI use, opt-in for long-lived readers like MCP and
    watchers), exit the process so a supervisor restarts it with fresh
    ChromaDB state. Without that env, the cached client is returned as-is
    — queries against on-disk segments mutated by another process may
    return stale results or SIGSEGV in the Rust _query path.
    """
    key = str(Path(db_path).expanduser().resolve())
    with _lock:
        if db_path in _clients:
            opened_at = _client_open_generation.get(key, 0.0)
            current = _read_generation(key)
            if current > opened_at:
                if _exit_on_stale_enabled():
                    print(
                        f"[llmli][chroma_client] writer activity detected on "
                        f"{db_path} (gen {opened_at:.6f} → {current:.6f}); "
                        f"exiting (99) so supervisor restarts with fresh state.",
                        file=sys.stderr,
                        flush=True,
                    )
                    sys.exit(99)
            return _clients[db_path]
        _storage_preflight(db_path)
        raw = chromadb.PersistentClient(
            path=db_path,
            settings=Settings(anonymized_telemetry=False),
        )
        _clients[db_path] = _SafeClient(raw)
        _client_open_generation[key] = _read_generation(key)
        return _clients[db_path]


def _exit_on_stale_enabled() -> bool:
    flag = os.environ.get("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", "").strip().lower()
    return flag in ("1", "true", "yes")


def release() -> None:
    """Release the Python-side client references after write operations.

    We intentionally do NOT call clear_system_cache() here. On ChromaDB 1.4+
    that call tears down the Rust/tokio runtime while background threads are
    still live, causing a SIGSEGV (KERN_INVALID_ADDRESS) on the next access.
    Dropping the Python reference is sufficient — the Rust destructor will
    drain its thread pool before freeing memory.
    """
    with _lock:
        _clients.clear()
        _client_open_generation.clear()


def get_collection(db_path: str, name: str, embedding_function=None):
    """Convenience wrapper: get-or-create a collection on the shared client."""
    return get_client(db_path).get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
    )


@contextmanager
def writer_client(db_path: str) -> Iterator["_SafeClient"]:
    """Acquire exclusive Chroma write access with a fresh, non-singleton client.

    Why fresh: get_client() caches a PersistentClient per process. Two writer
    processes can each hold a live cached client across overlapping flock windows
    (since the flock only wraps the call, not the client lifetime). With Chroma
    running an async compaction thread, two live clients on the same persist dir
    can corrupt the HNSW segment ("Failed to apply logs to the hnsw segment writer").

    By opening a dedicated client inside the exclusive lock and dropping it before
    the lock releases, only one writer client is alive on this DB at a time.
    Read paths continue to use the singleton via get_client() + chroma_shared_lock.
    """
    from chroma_lock import chroma_exclusive_lock

    with chroma_exclusive_lock(db_path):
        _storage_preflight(db_path)
        raw = chromadb.PersistentClient(
            path=db_path,
            settings=Settings(anonymized_telemetry=False),
        )
        client = _SafeClient(raw)
        try:
            yield client
        finally:
            del client
            del raw
            # Notify other-process readers that on-disk segments have moved
            # so they can self-restart with fresh ChromaDB state.
            bump_generation(db_path)
