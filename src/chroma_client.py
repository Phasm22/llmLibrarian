"""
Singleton ChromaDB client factory.

All modules import get_client() instead of constructing PersistentClient
directly. A single shared client per db_path eliminates the concurrent-write
SIGSEGV caused by multiple Rust HNSW handles on the same files.

When LLMLIBRARIAN_CHROMA_HOST is set, clients use chromadb.HttpClient against a
local ``chroma run`` server (single on-disk writer). Otherwise embedded
PersistentClient mode applies (not safe for concurrent processes on one path).
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import chromadb
from chromadb.config import Settings

from constants import env_flag, mcp_auth_token
from watch_locks import active_watchers_for_db

_lock = threading.Lock()
_clients: dict[str, "_SafeClient"] = {}
_fallback_warned: set[str] = set()

# Rate-limit the heartbeat probe fired from get_client() in HTTP mode. Without
# this every get_client() call (many per query/ingest) round-trips to the
# chroma server, and repeated open-then-close on the underlying httpx pool
# starves the ephemeral-port range under load.
_heartbeat_ok_at: dict[str, float] = {}


def _heartbeat_min_interval() -> float:
    raw = os.environ.get("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", "").strip()
    if not raw:
        return 5.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


# Persistent keep-alive HTTP connections used for the cheap reachability probes
# (Chroma /heartbeat, MCP /healthz). http.client opens a fresh socket per
# request, and each closed socket sits in TIME_WAIT ~60s -- issued from a hot
# code path this exhausts the ephemeral port range. We keep one HTTPConnection
# per (host, port, ssl) target under a lock, retry once on stale sockets.
_probe_lock = threading.Lock()
_probe_conns: dict[tuple[str, int, bool], http.client.HTTPConnection] = {}


def _make_probe_conn(host: str, port: int, ssl: bool, timeout: float) -> http.client.HTTPConnection:
    if ssl:
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _drop_probe_conn(key: tuple[str, int, bool]) -> None:
    conn = _probe_conns.pop(key, None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _probe_http(
    host: str,
    port: int,
    ssl: bool,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
) -> tuple[int, bytes, str | None]:
    """Send GET ``path`` on a pooled keep-alive HTTP connection.

    Returns ``(status, body, error)``. ``status == 0`` indicates a transport
    error (see ``error`` for the reason). Retries once on a stale socket so
    long-idle pooled connections don't surface transient failures to callers.
    """
    key = (host, port, ssl)
    hdrs = dict(headers or {})
    hdrs.setdefault("Connection", "keep-alive")
    with _probe_lock:
        last_err: str | None = None
        for attempt in (0, 1):
            conn = _probe_conns.get(key)
            if conn is None:
                conn = _make_probe_conn(host, port, ssl, timeout)
                _probe_conns[key] = conn
            else:
                conn.timeout = timeout
            try:
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                status = resp.status
                body = resp.read()
                if resp.will_close:
                    _drop_probe_conn(key)
                return status, body, None
            except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as exc:
                last_err = str(exc)
                _drop_probe_conn(key)
                if attempt == 0:
                    continue
                return 0, b"", last_err
        return 0, b"", last_err or "probe failed"


def _close_probe_pool() -> None:
    """Drop all pooled probe connections. Test/reset helper."""
    with _probe_lock:
        keys = list(_probe_conns.keys())
        for key in keys:
            _drop_probe_conn(key)

# Cross-process write-generation tracking (embedded mode only).
#
# Background: ChromaDB 1.4+ keeps a process-global Rust/tokio runtime cache.
# Calling clear_system_cache() to flush it crashes (see release() docstring).
# So once a PersistentClient is opened in this process, the on-disk segments
# it cached can be silently invalidated by another process's writer
# (op_repair_silo, run_add via writer_client). The next query then either
# returns garbage or SIGSEGVs in the Rust _query path.
#
# Mitigation: writer_client touches `.llmli_chroma_generation` after each
# successful write. Readers stash the file mtime at client-open time; when
# get_client() sees the file has moved it takes one of two corrective actions:
#
#   - LLMLIBRARIAN_EXIT_ON_STALE_GENERATION set (the MCP server defaults it on):
#     exit 99 so the supervisor restarts the process with fresh state.
#   - otherwise (CLI, pal, watchers): drop the cached client and reopen, the
#     same recovery the HTTP heartbeat path performs on a dropped connection.
#
# Either way the stale client is never handed back to a caller.
_GEN_FILE_NAME = ".llmli_chroma_generation"
_client_open_generation: dict[str, float] = {}


def chroma_transport_mode() -> str:
    """Return ``http`` when LLMLIBRARIAN_CHROMA_HOST is set, else ``embedded``."""
    if os.environ.get("LLMLIBRARIAN_CHROMA_HOST", "").strip():
        return "http"
    return "embedded"


def is_http_mode() -> bool:
    return chroma_transport_mode() == "http"


def chroma_http_settings() -> tuple[str, int, bool]:
    host = os.environ.get("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("LLMLIBRARIAN_CHROMA_PORT", "8000").strip() or "8000"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8000
    return host, port, env_flag("LLMLIBRARIAN_CHROMA_SSL")


def chroma_mode_info() -> dict[str, Any]:
    """Summary for MCP health() and operator tooling."""
    mode = chroma_transport_mode()
    out: dict[str, Any] = {
        "chroma_transport": mode,
        "embedded_write_unsafe_with_cli": mode == "embedded",
    }
    if mode == "http":
        host, port, ssl = chroma_http_settings()
        ok, detail = check_chroma_server_reachable(host, port, ssl=ssl)
        out["chroma_server_host"] = host
        out["chroma_server_port"] = port
        out["chroma_server_ok"] = ok
        if detail:
            out["chroma_server_detail"] = detail
    return out


def check_chroma_server_reachable(
    host: str | None = None,
    port: int | None = None,
    *,
    ssl: bool | None = None,
    timeout: float = 2.0,
) -> tuple[bool, str | None]:
    """Probe Chroma HTTP heartbeat. Returns (ok, error_detail).

    Uses the pooled keep-alive probe connection so repeated status checks
    don't churn ephemeral ports.
    """
    h, p, use_ssl = chroma_http_settings()
    if host is not None:
        h = host
    if port is not None:
        p = port
    if ssl is not None:
        use_ssl = ssl
    # Chroma 1.x serves heartbeat on v2; v1 returns 410 Gone.
    for path in ("/api/v2/heartbeat", "/api/v1/heartbeat"):
        status, _, err = _probe_http(h, p, use_ssl, path, timeout=timeout)
        if err is not None:
            return False, err
        if status == 200:
            return True, None
        if status == 410 and path == "/api/v1/heartbeat":
            continue
        return False, f"heartbeat {path} returned HTTP {status}"
    return False, "heartbeat unreachable"


def _mcp_healthz_info(timeout: float = 1.0) -> tuple[bool, str | None, bool]:
    """Probe the llmLibrarian MCP HTTP /healthz endpoint.

    Returns ``(reachable, db_path, auth_blocked)``. ``auth_blocked`` is True when
    the server answered but rejected our credentials -- a live MCP process we
    cannot identify, which is very different from nothing listening at all.

    Uses the pooled keep-alive probe connection.
    """
    host = os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("LLMLIBRARIAN_MCP_PORT", "8765").strip() or "8765"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8765
    headers: dict[str, str] = {}
    tok = mcp_auth_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    status, body, err = _probe_http(host, port, False, "/healthz", headers=headers, timeout=timeout)
    if err is not None:
        return False, None, False
    if status in (401, 403):
        return True, None, True
    if status != 200:
        return False, None, False
    raw = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return True, None, False
    if not isinstance(payload, dict) or not payload.get("ok"):
        return False, None, False
    db_raw = payload.get("db_path")
    if isinstance(db_raw, str) and db_raw.strip():
        return True, str(Path(db_raw).expanduser().resolve()), False
    return True, None, False


def _mcp_blocks_embedded_write(db_path: str) -> str | None:
    """Reason to refuse an embedded write because of a live MCP server, else None.

    An authenticated probe that names this db_path is a definite block. A probe
    rejected for bad credentials is also a block: a live MCP process is holding
    *some* DB open and we have no way to rule out this one. Guessing wrong
    SIGSEGVs, so the ambiguous case fails closed and says how to fix it.

    A 200 response without a db_path field is genuine version skew against an
    older server; that stays permissive.
    """
    up, mcp_db, auth_blocked = _mcp_healthz_info()
    if not up:
        return None
    if auth_blocked:
        return (
            "llmLibrarian MCP HTTP server is running but rejected our /healthz credentials, "
            "so it cannot be confirmed to be on a different DB. Set LLMLIBRARIAN_MCP_AUTH_TOKEN "
            "to the server's token, or set LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT=1 if you "
            "know the MCP server holds a different database"
        )
    if mcp_db is None:
        # Older MCP whose /healthz omits db_path — cannot confirm, stay permissive.
        return None
    if mcp_db == str(Path(db_path).expanduser().resolve()):
        return "llmLibrarian MCP HTTP server is running on this DB (holds a cached PersistentClient)"
    return None


def preflight_embedded_write(db_path: str) -> str | None:
    """Return an error message if an embedded write is likely to SIGSEGV, else None.

    Skipped in HTTP mode (Chroma server is the single on-disk writer).
    Skipped when LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT=1 (tests).
    """
    if is_http_mode():
        return None
    if env_flag("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT"):
        return None

    reasons: list[str] = []
    mcp_reason = _mcp_blocks_embedded_write(db_path)
    if mcp_reason:
        reasons.append(mcp_reason)
    reasons.extend(active_watchers_for_db(db_path))

    if not reasons:
        return None

    host, port, _ = chroma_http_settings()
    lines = [
        "Refusing embedded ChromaDB write: another long-lived process may have the index open.",
        "Opening a second PersistentClient on the same path can SIGSEGV (ChromaDB 1.x is not process-safe).",
        "",
        "Detected:",
    ]
    lines.extend(f"  - {r}" for r in reasons)
    lines.extend(
        [
            "",
            "Options:",
            "  - Route writes through MCP: add_silo / trigger_reindex / repair_silo",
            "  - Stop MCP and watchers, run pal pull / llmli add, then restart",
            f"  - Enable server mode: LLMLIBRARIAN_CHROMA_HOST=127.0.0.1 LLMLIBRARIAN_CHROMA_PORT={port}",
            "    then: pal chroma install && pal chroma start",
        ]
    )
    return "\n".join(lines)


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

    Idempotent. Safe across processes (uses filesystem mtime). A reader that
    opened its PersistentClient before this call will, on its next
    get_client(), either exit for a supervisor restart or drop and reopen —
    see the write-generation comment above. No-op in HTTP mode.
    """
    if is_http_mode():
        return
    p = _generation_path(db_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        os.utime(p, None)
    except OSError:
        pass


HNSW_BLOAT_BYTES = 1 << 30
_MIN_FREE_BYTES = 512 * 1024 * 1024

# Persist dirs whose storage preflight already passed in this process. The scan
# walks the whole persist directory, and get_client() plus writer_client() both
# want it on the same path within one operation.
_storage_preflight_ok: set[str] = set()


def find_bloated_hnsw(root: Path | str) -> tuple[Path, int] | None:
    """First ``link_lists.bin`` under ``root`` exceeding HNSW_BLOAT_BYTES.

    A multi-gigabyte link_lists.bin means the HNSW writer ran concurrently from
    two processes; the index is unusable and must be rebuilt.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return None
    for dirpath, _dirnames, filenames in os.walk(root):
        if "link_lists.bin" not in filenames:
            continue
        fp = Path(dirpath) / "link_lists.bin"
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size > HNSW_BLOAT_BYTES:
            return fp, size
    return None


def _storage_preflight(db_path: str) -> None:
    """Fail before opening Chroma when the persist directory is visibly unsafe."""
    root = Path(db_path).expanduser().resolve()
    if str(root) in _storage_preflight_ok:
        return
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
    bloated = find_bloated_hnsw(root)
    if bloated is not None:
        fp, size = bloated
        raise RuntimeError(
            f"ChromaDB storage preflight failed: bloated HNSW index {fp} is {size} bytes. "
            "Stop llmLibrarian writers and rebuild my_brain_db before querying or indexing."
        )
    _storage_preflight_ok.add(str(root))


def _open_raw_client(db_path: str) -> Any:
    if is_http_mode():
        host, port, ssl = chroma_http_settings()
        ok, detail = check_chroma_server_reachable(host, port, ssl=ssl)
        if not ok:
            raise RuntimeError(
                f"Chroma HTTP server not reachable at {host}:{port} ({detail}). "
                "Start it with: pal chroma start"
            )
        return chromadb.HttpClient(
            host=host,
            port=port,
            ssl=ssl,
            settings=Settings(anonymized_telemetry=False),
        )
    return chromadb.PersistentClient(
        path=db_path,
        settings=Settings(anonymized_telemetry=False),
    )


class _SafeClient:
    """Thin wrapper that retries get_or_create_collection with DefaultEmbeddingFunction
    when an embedding-function conflict is detected against an existing collection.

    This lets silos created before the mpnet upgrade continue to work without a
    full re-index. New silos still get the caller-supplied (mpnet) function.

    Tracks which EF was actually used per collection so callers can use the same
    one for explicit embedding (avoiding dimension mismatches in parallel ingest).
    """

    def __init__(self, client: Any) -> None:
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
                if key not in _fallback_warned and not env_flag("LLMLIBRARIAN_QUIET"):
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
    """Return (or create) the shared Chroma client for db_path.

    Embedded mode: PersistentClient with optional exit-on-stale generation.
    HTTP mode: HttpClient to a local ``chroma run`` server. In HTTP mode the
    cached client is re-validated with a cheap heartbeat probe so callers
    don't get ConnectError after the chroma server is restarted out from
    under us.
    """
    # One cache key for every map in this module. Callers spell the same
    # directory several ways ("./my_brain_db" vs the absolute path); keying on
    # the raw string would hand out two PersistentClients for one persist dir,
    # which is exactly the concurrent-handle SIGSEGV this module prevents.
    key = str(Path(db_path).expanduser().resolve())
    with _lock:
        if key in _clients:
            if is_http_mode():
                cached = _clients[key]
                now = time.monotonic()
                last_ok = _heartbeat_ok_at.get(key, 0.0)
                if now - last_ok < _heartbeat_min_interval():
                    return cached
                try:
                    cached._client.heartbeat()
                    _heartbeat_ok_at[key] = now
                    return cached
                except Exception:
                    # Stale connection (chroma server restarted). Drop and rebuild.
                    _clients.pop(key, None)
                    _heartbeat_ok_at.pop(key, None)
            else:
                opened_at = _client_open_generation.get(key, 0.0)
                current = _read_generation(key)
                if current <= opened_at:
                    return _clients[key]
                # Another process wrote since we opened. The cached client's
                # segments are stale either way; never return it.
                if _exit_on_stale_enabled():
                    print(
                        f"[llmli][chroma_client] writer activity detected on "
                        f"{db_path} (gen {opened_at:.6f} → {current:.6f}); "
                        f"exiting (99) so supervisor restarts with fresh state.",
                        file=sys.stderr,
                        flush=True,
                    )
                    sys.exit(99)
                _clients.pop(key, None)
                _client_open_generation.pop(key, None)
        _storage_preflight(key)
        raw = _open_raw_client(key)
        _clients[key] = _SafeClient(raw)
        if is_http_mode():
            _heartbeat_ok_at[key] = time.monotonic()
        else:
            _client_open_generation[key] = _read_generation(key)
        return _clients[key]


def _exit_on_stale_enabled() -> bool:
    if is_http_mode():
        return False
    return env_flag("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION")


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
        _heartbeat_ok_at.clear()
        _storage_preflight_ok.clear()
    _close_probe_pool()


@contextmanager
def writer_client(db_path: str) -> Iterator["_SafeClient"]:
    """Acquire exclusive Chroma write access.

    Embedded mode: fresh non-singleton PersistentClient inside flock; bumps generation.
    HTTP mode: shared HttpClient inside flock (server is single on-disk writer).
    """
    from chroma_lock import chroma_exclusive_lock

    err = preflight_embedded_write(db_path)
    if err:
        raise RuntimeError(err)

    with chroma_exclusive_lock(db_path):
        _storage_preflight(db_path)
        if is_http_mode():
            # Reuse the singleton: a second HttpClient would open a second
            # connection pool to the one server that already serializes writes.
            yield get_client(db_path)
        else:
            # _open_raw_client does not cache — get_client() owns that — so this
            # is a fresh handle the caller exclusively owns, built by the same
            # code path as every other client in this module.
            raw = _open_raw_client(db_path)
            client = _SafeClient(raw)
            try:
                yield client
            finally:
                del client
                del raw
                bump_generation(db_path)
