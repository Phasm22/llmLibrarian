"""
Singleton ChromaDB client factory.

All modules import get_client() instead of constructing PersistentClient
directly. A single shared client per db_path eliminates the concurrent-write
SIGSEGV caused by multiple Rust HNSW handles on the same files.
"""

import threading

import chromadb
from chromadb.config import Settings

_lock = threading.Lock()
_clients: dict[str, chromadb.PersistentClient] = {}


def get_client(db_path: str) -> chromadb.PersistentClient:
    """Return (or create) the shared PersistentClient for db_path."""
    with _lock:
        if db_path not in _clients:
            _clients[db_path] = chromadb.PersistentClient(
                path=db_path,
                settings=Settings(anonymized_telemetry=False),
            )
        return _clients[db_path]


def release() -> None:
    """Release all open ChromaDB handles. Call after write operations."""
    try:
        chromadb.PersistentClient.clear_system_cache()
    except Exception:
        pass
    with _lock:
        _clients.clear()


def get_collection(db_path: str, name: str, embedding_function=None):
    """Convenience wrapper: get-or-create a collection on the shared client."""
    return get_client(db_path).get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
    )
