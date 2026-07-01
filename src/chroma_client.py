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
# successful write. Readers stash the file mtime at client-open time, and
# check_for_writer_changes() reports True when the file has moved. Callers
# that detect this should exit (systemd will restart watchers; the MCP
# wrapper restarts itself via os.execv).
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
    ssl_flag = os.environ.get("LLMLIBRARIAN_CHROMA_SSL", "").strip().lower() in ("1", "true", "yes")
    return host, port, ssl_flag


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


def _mcp_healthz_info(timeout: float = 1.0) -> tuple[bool, str | None]:
    """Return (reachable, db_path) for the llmLibrarian MCP HTTP /healthz probe.

    Uses the pooled keep-alive probe connection.
    """
    host = os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("LLMLIBRARIAN_MCP_PORT", "8765").strip() or "8765"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8765
    headers: dict[str, str] = {}
    tok = os.environ.get("LLMLIBRARIAN_MCP_BEARER_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    status, body, err = _probe_http(host, port, False, "/healthz", headers=headers, timeout=timeout)
    if err is not None or status != 200:
        return False, None
    raw = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return True, None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return False, None
    db_raw = payload.get("db_path")
    if isinstance(db_raw, str) and db_raw.strip():
        return True, str(Path(db_raw).expanduser().resolve())
    return True, None


def _mcp_blocks_embedded_write(db_path: str) -> bool:
    """True when a live MCP server holds PersistentClient on this db_path."""
    up, mcp_db = _mcp_healthz_info()
    if not up:
        return False
    target = str(Path(db_path).expanduser().resolve())
    if mcp_db is None:
        # Cannot confirm MCP is on this DB (upgrade MCP /healthz includes db_path).
        return False
    return mcp_db == target


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _active_watch_processes_for_db(db_path: str) -> list[str]:
    """Return human-readable labels for running pal pull --watch processes on db_path."""
    db_resolved = str(Path(db_path).expanduser().resolve())
    pal_home = Path(os.environ.get("PAL_HOME", str(Path.home() / ".pal"))).expanduser()
    locks_dir = pal_home / "watch_locks"
    if not locks_dir.is_dir():
        return []
    active: list[str] = []
    for lock_path in sorted(locks_dir.glob("*.pid")):
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        lock_db = str(data.get("db_path") or "").strip()
        if lock_db and lock_db != db_resolved:
            continue
        pid_val = data.get("pid")
        try:
            pid = int(pid_val) if pid_val is not None else None
        except (TypeError, ValueError):
            pid = None
        if pid is None or not _pid_is_running(pid):
            continue
        silo = str(data.get("silo") or lock_path.stem)
        active.append(f"pal pull --watch (silo={silo}, pid={pid})")
    return active


def preflight_embedded_write(db_path: str) -> str | None:
    """Return an error message if an embedded write is likely to SIGSEGV, else None.

    Skipped in HTTP mode (Chroma server is the single on-disk writer).
    Skipped when LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT=1 (tests).
    """
    if is_http_mode():
        return None
    if os.environ.get("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return None

    reasons: list[str] = []
    if _mcp_blocks_embedded_write(db_path):
        reasons.append("llmLibrarian MCP HTTP server is running on this DB (holds a cached PersistentClient)")
    watchers = _active_watch_processes_for_db(db_path)
    reasons.extend(watchers)

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

    Idempotent. Safe across processes (uses filesystem mtime). Readers that
    opened their PersistentClient before this call will see
    check_for_writer_changes() == True on their next check.
    No-op in HTTP mode.
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


def check_for_writer_changes(db_path: str) -> bool:
    """Return True if a writer has bumped the generation since this process
    opened the cached PersistentClient for db_path. Returns False if no
    client is cached for this db_path (nothing to invalidate yet)."""
    if is_http_mode():
        return False
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
    """Return (or create) the shared Chroma client for db_path.

    Embedded mode: PersistentClient with optional exit-on-stale generation.
    HTTP mode: HttpClient to a local ``chroma run`` server. In HTTP mode the
    cached client is re-validated with a cheap heartbeat probe so callers
    don't get ConnectError after the chroma server is restarted out from
    under us.
    """
    key = str(Path(db_path).expanduser().resolve())
    with _lock:
        if db_path in _clients:
            if is_http_mode():
                cached = _clients[db_path]
                now = time.monotonic()
                last_ok = _heartbeat_ok_at.get(db_path, 0.0)
                if now - last_ok < _heartbeat_min_interval():
                    return cached
                try:
                    cached._client.heartbeat()
                    _heartbeat_ok_at[db_path] = now
                    return cached
                except Exception:
                    # Stale connection (chroma server restarted). Drop and rebuild.
                    _clients.pop(db_path, None)
                    _heartbeat_ok_at.pop(db_path, None)
            else:
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
        raw = _open_raw_client(db_path)
        _clients[db_path] = _SafeClient(raw)
        if is_http_mode():
            _heartbeat_ok_at[db_path] = time.monotonic()
        else:
            _client_open_generation[key] = _read_generation(key)
        return _clients[db_path]


def _exit_on_stale_enabled() -> bool:
    if is_http_mode():
        return False
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
        _heartbeat_ok_at.clear()
    _close_probe_pool()


def get_collection(db_path: str, name: str, embedding_function=None):
    """Convenience wrapper: get-or-create a collection on the shared client."""
    return get_client(db_path).get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
    )


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
            client = get_client(db_path)
            try:
                yield client
            finally:
                pass
        else:
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
                bump_generation(db_path)
