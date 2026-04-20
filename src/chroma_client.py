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
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

_lock = threading.Lock()
_clients: dict[str, "_SafeClient"] = {}
_fallback_warned: set[str] = set()

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
    """Return (or create) the shared PersistentClient for db_path."""
    with _lock:
        if db_path not in _clients:
            _storage_preflight(db_path)
            raw = chromadb.PersistentClient(
                path=db_path,
                settings=Settings(anonymized_telemetry=False),
            )
            _clients[db_path] = _SafeClient(raw)
        return _clients[db_path]


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


def get_collection(db_path: str, name: str, embedding_function=None):
    """Convenience wrapper: get-or-create a collection on the shared client."""
    return get_client(db_path).get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
    )
