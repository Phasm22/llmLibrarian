import atexit
import errno
import logging
import os
import signal
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version as package_version
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ── MCP stdio guard ──────────────────────────────────────────────────────────
# This process communicates with the MCP client over stdout (JSON-RPC).
# Any non-JSON written to stdout corrupts the protocol stream.
# Suppress progress/warning output from HF Hub, tqdm, and transformers
# BEFORE any library imports that might trigger model loading.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TQDM_DISABLE", "1")
# Long-lived MCP process: when another process bumps the Chroma write
# generation, exit so the MCP client restarts us with fresh ChromaDB state.
# Avoids the cross-process Rust HNSW SIGSEGV when on-disk segments are
# mutated under a cached PersistentClient.
os.environ.setdefault("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", "1")
# ─────────────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

def _bootstrap_process_env() -> None:
    try:
        from env_bootstrap import bootstrap_llmlibrarian_env

        bootstrap_llmlibrarian_env(repo_root=_ROOT)
    except Exception:
        return


_bootstrap_process_env()

_logger = logging.getLogger("llmLibrarian.mcp")


def _looks_like_checkout(path: Path) -> bool:
    return (path / "cli.py").exists() and (path / "src").is_dir()


def _iter_editable_roots(site_root: Path) -> list[Path]:
    roots: list[Path] = []
    for pth_path in sorted(site_root.glob("*llmlibrarian*.pth")):
        try:
            for raw_line in pth_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                candidate = Path(line).expanduser()
                if candidate.exists():
                    roots.append(candidate.resolve())
        except Exception:
            continue
    return roots


def _resolve_db_path() -> str:
    """Resolve DB path, falling back gracefully when env vars contain unresolved templates."""
    raw = os.environ.get("LLMLIBRARIAN_DB", "")
    # Detect unresolved Claude Desktop template variables like ${user.db_path}
    if raw and not raw.startswith("${"):
        return str(Path(raw).resolve())

    # Try to read from Claude Desktop extension settings file
    _settings_candidates = [
        Path.home() / "Library/Application Support/Claude/Claude Extensions Settings/local.mcpb.tjm4.llmlibrarian.json",
    ]
    for settings_file in _settings_candidates:
        if settings_file.exists():
            try:
                import json
                cfg = json.loads(settings_file.read_text())
                db_path = cfg.get("userConfig", {}).get("db_path", "")
                if db_path and not db_path.startswith("${"):
                    return str(Path(db_path).resolve())
            except Exception:
                pass

    # Fall back to the active checkout before script root. When mcp_server.py is
    # installed into site-packages, script-root fallback creates a hidden DB in
    # .venv; that DB is easy to miss and can bloat.
    fallback_candidates = [
        Path.cwd() / "my_brain_db" if _looks_like_checkout(Path.cwd()) else None,
        *[root / "my_brain_db" for root in _iter_editable_roots(_ROOT) if _looks_like_checkout(root)],
        _ROOT / "my_brain_db" if _looks_like_checkout(_ROOT) else None,
        Path.home() / "llmLibrarian" / "my_brain_db",
    ]
    for candidate in fallback_candidates:
        if candidate is not None and candidate.exists():
            return str(candidate.resolve())

    cwd = Path.cwd().resolve()
    if _looks_like_checkout(cwd):
        return str((cwd / "my_brain_db").resolve())
    for root in _iter_editable_roots(_ROOT):
        if _looks_like_checkout(root):
            return str((root / "my_brain_db").resolve())
    return str((_ROOT / "my_brain_db").resolve())


_DB_PATH = _resolve_db_path()
_CONFIG_PATH = str(Path(os.environ.get("LLMLIBRARIAN_CONFIG", str(_ROOT / "archetypes.yaml"))).resolve())

import threading

# Serializes concurrent trigger_reindex calls in-process (no subprocess races).
_reindex_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Single-instance enforcement (HTTP transport only)
# fcntl.LOCK_EX | LOCK_NB on a PID file prevents a second HTTP server from
# opening the same ChromaDB path and corrupting the HNSW index.
# Stdio processes are ephemeral and do not acquire this lock.
# ---------------------------------------------------------------------------

_server_lock_fd: int | None = None
_server_lock_path: Path | None = None
_SERVER_STARTED_AT: str | None = None


def _package_version() -> str:
    try:
        return package_version("llmlibrarian")
    except PackageNotFoundError:
        return "unknown"


def _pid_file_path() -> Path:
    # Use a fixed path that doesn't depend on XDG_RUNTIME_DIR so shell-launched
    # and service-launched processes always agree on the same lock file.
    uid = os.getuid() if hasattr(os, "getuid") else "0"
    return Path(f"/tmp/llmlibrarian-mcp-{uid}.pid")


def _release_server_lock() -> None:
    global _server_lock_fd, _server_lock_path
    fd, path = _server_lock_fd, _server_lock_path
    _server_lock_fd = None
    _server_lock_path = None
    if fd is not None:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _sigterm_handler(signum: int, frame: object) -> None:
    _release_server_lock()
    sys.exit(0)


def _acquire_server_lock() -> None:
    """Acquire an exclusive PID file lock; exit(0) cleanly if already held."""
    global _server_lock_fd, _server_lock_path
    try:
        import fcntl
    except ImportError:
        return  # Windows — skip (fcntl is Unix-only)

    pid_path = _pid_file_path()
    try:
        fd = os.open(str(pid_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        print(f"[llmLibrarian] cannot open PID file {pid_path}: {e}", file=sys.stderr)
        return

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno not in (errno.EWOULDBLOCK, errno.EACCES):
            os.close(fd)
            raise
        # Lock held by another process — read its PID and exit cleanly.
        try:
            existing = os.pread(fd, 32, 0).decode().strip()
        except Exception:
            existing = "unknown"
        os.close(fd)
        print(
            f"[llmLibrarian] another MCP server is already running (pid={existing}), exiting",
            file=sys.stderr,
        )
        sys.exit(0)

    # Lock acquired — record fd and write our PID.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _server_lock_fd = fd
    _server_lock_path = pid_path
    atexit.register(_release_server_lock)
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

if not Path(_DB_PATH).exists():
    print(
        f"[llmLibrarian WARNING] DB path does not exist: {_DB_PATH}\n"
        f"Set LLMLIBRARIAN_DB to your my_brain_db directory.",
        file=sys.stderr,
    )

from fastmcp import FastMCP
from fastmcp.server.auth.auth import AuthProvider
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class StaticBearerTokenAuth(AuthProvider):
    """Simple static bearer token verifier for MCP HTTP transports."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token.strip()

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return AccessToken(
            token=token,
            client_id="llmli-mcp-client",
            scopes=["mcp"],
        )


def _auth_for_transport(transport: str) -> AuthProvider | None:
    if transport == "stdio":
        return None
    require_auth = _env_bool("LLMLIBRARIAN_MCP_REQUIRE_AUTH", False)
    if not require_auth:
        return None
    token = os.environ.get("LLMLIBRARIAN_MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_AUTH_TOKEN is required when "
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH=true."
        )
    return StaticBearerTokenAuth(token)

mcp = FastMCP(
    name="llmLibrarian",
    instructions=(
        "Use these tools when a task requires context from the user's personal knowledge base. "
        "At session start, call session_context(check_staleness=True) for roster + health summary + actions. "
        "If you only need a quick roster, call list_silos. Do not assume a silo's topic from its slug alone; names "
        "can drift or be reused, so verify with list_silos metadata and retrieved sources. "
        ""
        "If retrieval returns zero chunks with no error, cross-check list_silos before treating "
        "that as evidence of absence: chunks_count > 0, has_index_errors, or has_ingest_failures "
        "means the empty result may be an index/tool problem. Call session_context or health for diagnostics. "
        ""
        "For any task involving the user's past thinking, decisions, habits, or writing, "
        "call query_personal_knowledge before responding. Use multi_query_knowledge when "
        "a task needs multiple angles of context simultaneously. "
        ""
        "multi_query_knowledge caps merged output at max_total_chunks (default 50) — if "
        "truncated=True is returned, lower n_results or reduce the number of queries. "
        "Pass section= to scope retrieval to a document section. Pass doc_type= to "
        "restrict by file type (e.g. 'transcript', 'resume', 'tax_return', 'code'). "
        "For tax queries, query_personal_knowledge also returns a tax_ledger field with structured "
        "extracted values (AGI, total tax, W-2 boxes). Prefer tax_ledger over raw "
        "chunks for precise figures. "
        ""
        "Use inspect_silo to diagnose coverage gaps. Use watch_coverage to check whether "
        "registered sources have watcher jobs, but do not assume watchers are active. "
        "Use mcp_runtime_status when lock/process/runtime visibility is unclear (multiple MCP PIDs, stale pid lock, or transport confusion). "
        "Use trigger_reindex after file changes or when list_silos shows an old updated "
        "timestamp for a silo with active source changes. Use repair_silo for Chroma "
        "corruption. Use health for server diagnostics. Use capabilities for supported "
        "file types."
    ),
)


# ---------------------------------------------------------------------------
# Serializes ALL ChromaDB client use across concurrent MCP tool calls (in-process).
# Cross-process safety uses flock in src/chroma_lock.py (shared reads, exclusive writes).
# ChromaDB's Rust HNSW writer is not safe for concurrent use — simultaneous
# reads and background reindex writes corrupted link_lists.bin to 680 GB.
_chroma_lock = threading.Lock()


def _mcp_lock_timeout_seconds() -> float:
    raw = os.environ.get("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "").strip()
    if not raw:
        raw = os.environ.get("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 10.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 10.0


@contextmanager
def _mcp_chroma_lock(operation: str):
    timeout = _mcp_lock_timeout_seconds()
    if not _chroma_lock.acquire(timeout=timeout):
        raise TimeoutError(
            f"Timed out after {timeout:g}s waiting for MCP Chroma lock during {operation}. "
            "A background reindex or another tool call is still using Chroma; call health() "
            "for last_background_reindex, then retry or restart the stuck MCP server."
        )
    try:
        yield
    finally:
        _chroma_lock.release()

# Last background reindex outcome per silo (for health / debugging).
_reindex_outcome_lock = threading.Lock()
_last_reindex_outcome: dict[str, dict] = {}
_active_background_jobs: dict[str, dict] = {}


def _mark_background_job_started(key: str, *, kind: str, path: str, silo: str | None = None) -> None:
    with _reindex_outcome_lock:
        _active_background_jobs[key] = {
            "kind": kind,
            "silo": silo or key,
            "path": path,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }


def _mark_background_job_finished(key: str, outcome: dict) -> None:
    with _reindex_outcome_lock:
        _active_background_jobs.pop(key, None)
        _last_reindex_outcome[key] = outcome


def _release_chroma() -> None:
    try:
        from chroma_client import release

        release()
    except Exception:
        pass


def _db_missing_error() -> dict:
    return {
        "db_path": _DB_PATH,
        "db_exists": False,
        "error": (
            f"LLMLIBRARIAN_DB does not exist: {_DB_PATH}. "
            "Fix the MCP environment/config or create/re-index the DB before using knowledge retrieval."
        ),
    }


# Helper: answer-level confidence signal
# ---------------------------------------------------------------------------

def _compute_answer_confidence(chunks: list[dict]) -> tuple[str, float, str]:
    scores = [c["score"] for c in chunks[:10] if c.get("score") is not None]
    if not scores:
        return "low", 0.0, "no chunks returned"
    top = scores[0]
    mean = sum(scores) / len(scores)
    high_count = sum(1 for s in scores if s >= 0.5)
    sources = len({c.get("source", "") for c in chunks[:10]})
    if top >= 0.6 and mean >= 0.4:
        level = "high"
        note = f"{high_count} high-confidence chunk(s) from {sources} source(s)"
    elif top >= 0.4:
        level = "medium"
        note = f"moderate match — top score {top:.2f}, {sources} source(s)"
    else:
        level = "low"
        note = "sparse match — consider a broader or rephrased query"
    return level, round(mean, 4), note


def _collect_health_summary(*, include_audit: bool = False) -> dict:
    """Collect health signals used by health() and session_context().

    include_audit=False keeps payload small for routine session bootstrap.
    include_audit=True includes deeper audit/storage details used by health().
    """
    from chroma_client import chroma_mode_info
    from operations import op_db_storage_summary, op_silo_hnsw_consistency
    from silo_audit import (
        find_count_mismatches,
        find_duplicate_hashes,
        find_orphaned_sources,
        find_path_overlaps,
        load_file_registry,
        load_manifest,
        load_registry,
    )
    from state import get_last_failures, get_query_health

    query_errors = get_query_health(_DB_PATH)
    last_failures = get_last_failures(_DB_PATH)

    out: dict = {
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        **chroma_mode_info(),
        "embedding_model": os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2"),
        "embedding_kind": os.environ.get("LLMLIBRARIAN_EMBEDDING", "") or "sentence_transformer",
        "python_version": sys.version,
        "query_health": {
            "recent_error_count": len(query_errors),
            **({"recent_errors": query_errors[-10:]} if include_audit else {}),
        },
        "ingest_failures": {
            "last_failure_count": len(last_failures),
            **({"last_failures": last_failures[:20]} if include_audit else {}),
        },
    }

    if Path(_DB_PATH).is_dir():
        try:
            hnsw = op_silo_hnsw_consistency(_DB_PATH)
            if hnsw.get("status") == "ok":
                bad = [r for r in hnsw.get("silos") or [] if not r.get("consistent")]
                if include_audit:
                    out["hnsw_consistency"] = {
                        "silo_count": hnsw.get("silo_count", 0),
                        "desynced_count": hnsw.get("desynced_count", 0),
                        "desynced": [
                            {
                                "slug": r["slug"],
                                "sqlite_ids": r["sqlite_ids"],
                                "missing_count": r["missing_count"],
                                "queued": r["queued"],
                                "missing_ids_sample": r["missing_ids"][:5],
                            }
                            for r in bad
                        ],
                    }
                else:
                    out["hnsw_consistency"] = {
                        "silo_count": hnsw.get("silo_count", 0),
                        "desynced_count": hnsw.get("desynced_count", 0),
                    }
            else:
                out["hnsw_consistency"] = {"error": hnsw.get("error")}
        except Exception as e:
            out["hnsw_consistency"] = {"error": f"{type(e).__name__}: {e}"}

    if include_audit:
        registry = load_registry(_DB_PATH)
        manifest = load_manifest(_DB_PATH)
        file_registry = load_file_registry(_DB_PATH)
        out["silo_audit"] = {
            "silo_count": len(registry),
            "count_mismatch_count": len(find_count_mismatches(registry, manifest)),
            "count_mismatches": find_count_mismatches(registry, manifest)[:20],
            "duplicate_hash_group_count": len(find_duplicate_hashes(file_registry)),
            "path_overlap_count": len(find_path_overlaps(registry)),
            "orphaned_source_count": len(find_orphaned_sources(registry)),
            "orphaned_sources": find_orphaned_sources(registry)[:20],
        }
        if Path(_DB_PATH).is_dir():
            out["storage"] = op_db_storage_summary(_DB_PATH)

    with _reindex_outcome_lock:
        out["active_background_jobs"] = dict(_active_background_jobs)
        out["last_background_reindex"] = dict(_last_reindex_outcome)

    return out


def _derive_recommended_actions(silos: list[dict], summary: dict) -> list[str]:
    """Generate concise operational guidance for session bootstrap."""
    actions: list[str] = []

    if not summary.get("db_exists"):
        actions.append("Fix LLMLIBRARIAN_DB for this MCP process before retrieval.")
        return actions

    if summary.get("chroma_transport") == "http" and not summary.get("chroma_server_ok", True):
        host = summary.get("chroma_server_host", "127.0.0.1")
        port = summary.get("chroma_server_port", 8000)
        actions.append(f"Chroma HTTP appears down at {host}:{port}; start/restart it (for example: pal chroma start).")

    hnsw = summary.get("hnsw_consistency") or {}
    if isinstance(hnsw, dict):
        if int(hnsw.get("desynced_count", 0) or 0) > 0:
            actions.append("One or more silos are HNSW-desynced; run repair_silo on affected silos.")
        elif hnsw.get("error"):
            actions.append("HNSW consistency check returned an error; call health() for full diagnostics.")

    if int(((summary.get("query_health") or {}).get("recent_error_count", 0) or 0)) > 0:
        actions.append("Recent query index errors were recorded; check health() details and repair affected silos if needed.")
    if int(((summary.get("ingest_failures") or {}).get("last_failure_count", 0) or 0)) > 0:
        actions.append("Recent ingest failures exist; inspect failures and reindex/repair affected silos.")

    for s in silos:
        slug = str(s.get("slug") or "")
        if not slug:
            continue
        if bool(s.get("has_index_errors")):
            actions.append(f"Silo {slug} reports index errors; run repair_silo for a clean rebuild.")
        if bool(s.get("has_ingest_failures")):
            actions.append(f"Silo {slug} has ingest failures; inspect and reindex or repair.")
        if bool(s.get("is_stale")):
            stale_count = int(s.get("stale_file_count") or 0)
            newest = str(s.get("newest_source_mtime_iso") or "")
            updated = str(s.get("updated") or "")
            if stale_count <= 3 and newest and updated and newest == updated:
                actions.append(f"Silo {slug} shows minor stale noise (<=3 files) with matching timestamps; index is likely usable.")
            else:
                actions.append(f"Silo {slug} appears stale ({stale_count} files); run trigger_reindex once source changes settle.")

    active = summary.get("active_background_jobs") or {}
    if isinstance(active, dict) and active:
        actions.append("Background ingest/reindex jobs are active; wait for completion or check health() before heavy querying.")

    return actions


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check for local PIDs."""
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_mcp_pid_lock_snapshot() -> dict:
    """Return MCP PID lock visibility without raising."""
    path = _pid_file_path()
    out: dict = {
        "pid_lock_path": str(path),
        "lock_file_exists": path.exists(),
        "lock_holder_pid": None,
        "lock_holder_alive": None,
    }
    if not path.exists():
        return out
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw) if raw else None
        out["lock_holder_pid"] = pid
        if pid is not None:
            out["lock_holder_alive"] = _pid_is_alive(pid)
        return out
    except Exception as e:
        out["introspection_error"] = f"{type(e).__name__}: {e}"
        return out


def _mcp_process_snapshot(*, verbose: bool = False) -> dict:
    """Count mcp_server.py processes via /proc scanning (Linux) with graceful fallback."""
    out: dict = {"mcp_process_count": 0, "multiple_mcp_processes": False}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        out["introspection_error"] = "procfs unavailable"
        return out

    rows: list[dict] = []
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            cmdline_path = entry / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except Exception:
                continue
            if not raw:
                continue
            parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
            joined = " ".join(parts)
            if "mcp_server.py" in joined and "llmLibrarian" in joined:
                row = {"pid": int(entry.name)}
                if verbose:
                    row["cmdline"] = joined
                rows.append(row)
    except Exception as e:
        out["introspection_error"] = f"{type(e).__name__}: {e}"
        return out

    out["mcp_process_count"] = len(rows)
    out["multiple_mcp_processes"] = len(rows) > 1
    if verbose:
        out["processes"] = rows
    return out


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _compact_runtime_jobs(summary: dict, *, verbose: bool = False, limit: int = 5) -> dict:
    """Keep runtime jobs compact by default; expose full records in verbose mode."""
    active = summary.get("active_background_jobs") or {}
    last = summary.get("last_background_reindex") or {}
    if not isinstance(active, dict):
        active = {}
    if not isinstance(last, dict):
        last = {}

    if verbose:
        return {"active_background_jobs": active, "last_background_reindex": last}

    active_compact: dict[str, dict] = {}
    for key in list(active.keys())[:limit]:
        row = active.get(key) or {}
        if not isinstance(row, dict):
            continue
        active_compact[key] = {
            "kind": row.get("kind"),
            "silo": row.get("silo"),
            "started_at": row.get("started_at"),
        }

    last_compact: dict[str, dict] = {}
    for key in list(last.keys())[:limit]:
        row = last.get(key) or {}
        if not isinstance(row, dict):
            continue
        last_compact[key] = {
            "ok": row.get("ok"),
            "finished_at": row.get("finished_at"),
            **({"error": row.get("error")} if row.get("error") else {}),
        }

    return {
        "active_background_jobs": active_compact,
        "last_background_reindex": last_compact,
        "active_count": len(active),
        "last_outcome_count": len(last),
    }


# ---------------------------------------------------------------------------
# Helper: doc-type breakdown (delegated to operations module)
# ---------------------------------------------------------------------------

from operations import _doc_type_breakdown, _inject_staleness


# ---------------------------------------------------------------------------
# Usage telemetry (Argus usage-dashboard rollout — see argus/docs/
# usage-dashboard-rollout.md). Dedicated log file, separate from anything
# Argus's llmlibrarian.mcp liveness binding tails, so a quiet query week
# can't be misread as a liveness gap and vice versa. Best-effort: never let
# telemetry failures affect a query response.
# ---------------------------------------------------------------------------

import json as _json


def _usage_log_path() -> Path:
    import jobs_runtime as _jobsrt
    pal_home = Path(os.environ.get("PAL_HOME", str(Path.home() / ".pal"))).expanduser()
    return _jobsrt.watch_log_dir(pal_home) / "usage.log"


def _emit_usage_event(event: str, metrics: dict) -> None:
    try:
        path = _usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps({
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": metrics,
        })
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        _logger.debug("usage event emit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def query_personal_knowledge(
    query: str,
    silo: str | None = None,
    n_results: int = 40,
    section: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """
    Use when: answering a content/meaning question from indexed files.
    Do not use when: the user wants filenames/date filtering only (`find_files`) or ranking diagnostics only (`explain_retrieval`).
    Pairs with: `session_context`/`list_silos` first, then optional `inspect_silo`/`health` if coverage looks weak.

    Call this when a task requires the user's personal context — past writing,
    decisions, reflections, or domain knowledge. Returns semantically ranked
    chunks from indexed silos.

    Specify silo to scope retrieval by slug or display name; call list_silos first
    rather than inferring a silo's domain from its slug. Returns chunks with text, score (0–1),
    confidence, section heading, source path, date, doc_type, and position.

    Pass section= to restrict to a document section.
    Pass doc_type= to restrict by file type.
    Intent routing is applied automatically.
    For tax queries, also returns a tax_ledger field with structured extracted values.
    Response includes answer_confidence and coverage_note to calibrate hedging.
    When no silo filter is passed, also returns chunks_by_silo grouped by silo.
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "chunks": []}
    try:
        with _mcp_chroma_lock("query_personal_knowledge"):
            result = run_retrieve(
                query=query,
                silo=silo,
                n_results=n_results,
                section=section,
                doc_type=doc_type,
                db_path=_DB_PATH,
                config_path=_CONFIG_PATH,
            )
            chunks = result.get("chunks", [])

            _emit_usage_event("usage.llmlibrarian.query", {"silo": silo or "unscoped"})

            # Answer-level confidence signal
            conf_level, conf_score, coverage_note = _compute_answer_confidence(chunks)
            result["answer_confidence"] = conf_level
            result["answer_confidence_score"] = conf_score
            result["coverage_note"] = coverage_note

            # Retrieval signal summary — helps LLM calibrate how to weight chunks
            lexical_hits = sum(1 for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is not None)
            vector_hits = sum(1 for c in chunks if (c.get("_signals") or {}).get("vector_rank") is not None)
            result["lexical_hit_count"] = lexical_hits
            result["vector_hit_count"] = vector_hits

            # Cross-silo grouping (only when unscoped)
            if not silo and chunks:
                by_silo: dict[str, list] = {}
                for c in chunks:
                    by_silo.setdefault(c.get("silo", ""), []).append(c)
                result["chunks_by_silo"] = by_silo

            return {"db_path": _DB_PATH, **result}
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}", "chunks": []}
    finally:
        _release_chroma()


@mcp.tool()
def multi_query_knowledge(
    queries: list[str],
    silo: str | None = None,
    n_results: int = 20,
    section: str | None = None,
    doc_type: str | None = None,
    max_total_chunks: int = 50,
) -> dict:
    """
    Use when: one task needs multiple retrieval angles merged in one response.
    Do not use when: a single focused query is enough (`query_personal_knowledge`) or when you need silo discovery (`session_context`/`list_silos`).
    Pairs with: `session_context` first; follow up with `query_personal_knowledge` if truncation hides details.

    Call this when a task needs multiple angles of context from personal knowledge.
    Fire multiple semantic queries and return merged, deduplicated chunks ranked
    by best score. Use when a topic requires several retrieval angles — for example,
    to cover the user's thinking on related topics in one call instead of many.
    Each chunk is tagged with the query that retrieved it. Pass section= to restrict
    all queries to a document section. Pass doc_type= to restrict by file type
    (e.g. 'transcript', 'resume', 'tax_return', 'code', 'other').
    max_total_chunks caps the merged output (default 50) to avoid context overflow.
    If the cap is hit, response includes truncated=True — lower n_results or reduce queries.
    Response includes answer_confidence, answer_confidence_score, and coverage_note.
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "queries": queries, "total_chunks": 0, "chunks": []}
    seen: set[str] = set()
    all_chunks: list[dict] = []
    errors: list[str] = []
    for q in queries:
        try:
            with _mcp_chroma_lock("multi_query_knowledge"):
                res = run_retrieve(
                    query=q,
                    silo=silo,
                    n_results=n_results,
                    section=section,
                    doc_type=doc_type,
                    db_path=_DB_PATH,
                    config_path=_CONFIG_PATH,
                )
            for chunk in res.get("chunks", []):
                key = (chunk.get("text") or "")[:200]
                if key and key not in seen:
                    seen.add(key)
                    chunk["query"] = q
                    all_chunks.append(chunk)
        except Exception as e:
            errors.append(f"{q!r}: {type(e).__name__}: {e}")
    all_chunks.sort(key=lambda c: c.get("score") or 0, reverse=True)
    truncated = len(all_chunks) > max_total_chunks
    if truncated:
        all_chunks = all_chunks[:max_total_chunks]

    # Feature 6: answer-level confidence on merged results
    conf_level, conf_score, coverage_note = _compute_answer_confidence(all_chunks)

    _emit_usage_event(
        "usage.llmlibrarian.query",
        {"silo": silo or "unscoped", "query_count": len(queries)},
    )

    _release_chroma()
    return {
        "db_path": _DB_PATH,
        "queries": queries,
        "total_chunks": len(all_chunks),
        "truncated": truncated,
        "answer_confidence": conf_level,
        "answer_confidence_score": conf_score,
        "coverage_note": coverage_note,
        "chunks": all_chunks,
        **({"errors": errors} if errors else {}),
    }


@mcp.tool()
def explain_retrieval(
    query: str,
    silo: str | None = None,
    n_results: int = 20,
) -> dict:
    """
    Use when: debugging retrieval quality (hybrid/vector signal breakdown).
    Do not use when: generating user-facing answers; use `query_personal_knowledge`.
    Pairs with: `query_personal_knowledge`, `inspect_silo`, `health`.

    Return a structured breakdown of how retrieval results were ranked for a query.
    Useful for diagnosing missed results, unexpected rankings, or low confidence answers
    before deciding to re-query with different parameters.

    Returns:
    - retrieval_method: 'hybrid' (vector + lexical RRF) or 'vector_only'
    - lexical_hit_count: chunks with lexical_rank set (exact-text match)
    - vector_only_chunk_count: chunks with no lexical_rank (semantic-only in this result set)
    - chunk_with_vector_rank_count: chunks that participated in vector ranking (includes hybrid rows)
    - vector_hit_count: deprecated alias for vector_only_chunk_count
    - ranked_chunks: each chunk with its _signals (vector_rank, lexical_rank, rrf_score),
      score, source, and a short text preview
    - signal_summary: plain-text explanation of what signals fired and why
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "query": query, "ranked_chunks": []}
    try:
        with _mcp_chroma_lock("explain_retrieval"):
            result = run_retrieve(
                query=query,
                silo=silo,
                n_results=n_results,
                db_path=_DB_PATH,
                config_path=_CONFIG_PATH,
            )
            chunks = result.get("chunks", [])
            method = result.get("retrieval_method", "unknown")

            lexical_hits = [c for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is not None]
            vector_only_hits = [c for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is None]
            with_vector_rank = sum(
                1 for c in chunks if (c.get("_signals") or {}).get("vector_rank") is not None
            )

            ranked_chunks = []
            for c in chunks:
                sig = c.get("_signals") or {}
                ranked_chunks.append({
                    "rank": c.get("rank"),
                    "score": c.get("score"),
                    "source": c.get("source", ""),
                    "silo": c.get("silo", ""),
                    "text_preview": (c.get("text") or "")[:200],
                    "vector_rank": sig.get("vector_rank"),
                    "lexical_rank": sig.get("lexical_rank"),
                    "rrf_score": sig.get("rrf_score"),
                })

            # Build human-readable signal summary
            if method == "hybrid":
                summary_parts = [
                    f"Hybrid retrieval fired: {len(lexical_hits)} chunk(s) had exact-text matches (lexical_rank set); "
                    f"{with_vector_rank} chunk(s) have vector_rank (semantic ranking); "
                    f"{len(vector_only_hits)} chunk(s) are semantic-only in this set (no lexical_rank).",
                ]
                if lexical_hits:
                    top_lex = lexical_hits[0]
                    summary_parts.append(
                        f"Top lexical hit (rank {top_lex['rank']}): {top_lex.get('source','?')} "
                        f"— score {top_lex.get('score')}"
                    )
            else:
                summary_parts = [
                    f"Vector-only retrieval: no exact-text terms were extracted from the query or lexical search returned no results. "
                    f"All {len(chunks)} chunk(s) matched semantically."
                ]

            return {
                "db_path": _DB_PATH,
                "query": query,
                "intent": result.get("intent"),
                "retrieval_method": method,
                "lexical_hit_count": len(lexical_hits),
                "vector_only_chunk_count": len(vector_only_hits),
                "chunk_with_vector_rank_count": with_vector_rank,
                "vector_hit_count": len(vector_only_hits),
                "signal_summary": " ".join(summary_parts),
                "ranked_chunks": ranked_chunks,
            }
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}", "ranked_chunks": []}
    finally:
        _release_chroma()


@mcp.tool()
def watch_coverage() -> dict:
    """
    Use when: checking whether pal bookmarks map to daemon watch jobs/services.
    Do not use when: diagnosing index corruption/staleness (`session_context`, `health`, `trigger_reindex`, `repair_silo`).
    Pairs with: `list_silos` for source-to-silo sanity checks.

    Read-only: pal bookmarks (~/.pal/registry.json) vs indexed silos vs derived watch jobs.
    Returns bookmark rows (path, resolved_path, silo_slug, path_exists, indexed, would_watch),
    watch_jobs (what `pal daemon` would sync), service unit file presence when daemon is installed,
    warnings (skipped bookmarks), and indexed_not_bookmarked (llmli silos with no matching bookmark path).
    Does not start watchers or modify launchd/systemd.
    Unrelated to ChromaDB health: for corrupt/zero-chunk index issues use `repair_silo`; for stale content after edits use `trigger_reindex`.
    """
    from operations import op_watch_coverage
    return op_watch_coverage(_DB_PATH)


@mcp.tool()
def list_silos(check_staleness: bool = False) -> dict:
    """
    Use when: you need a live roster of registered silos before retrieval.
    Do not use when: you also need health diagnostics/action guidance — use `session_context`.
    Pairs with: `query_personal_knowledge`, `multi_query_knowledge`, `trigger_reindex`.

    List all indexed silos. Returns db_path (the database this server is using),
    db_exists (false means LLMLIBRARIAN_DB is misconfigured), silo_count, and
    a silos array with slug, display name, path, file count, chunk count, last-indexed
    timestamp, and doc_type_breakdown (counts by category: pdf/code/docx/xlsx/pptx/other).
    Use slugs with `query_personal_knowledge`/`multi_query_knowledge` to scope queries.
    Pass check_staleness=True to also get is_stale, stale_file_count, and
    newest_source_mtime_iso per silo (walks source directory — may be slow for large silos).
    """
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "silo_count": 0, "silos": []}
    from operations import op_list_silos
    return op_list_silos(_DB_PATH, check_staleness=check_staleness)


@mcp.tool()
def session_context(check_staleness: bool = True, include_audit: bool = False) -> dict:
    """
    Use when: starting a personal-knowledge task and you need one bootstrap call.
    Do not use when: you only need a quick silo roster (`list_silos`) or deep diagnostics (`health`).
    Pairs with: `query_personal_knowledge` / `multi_query_knowledge` after reviewing actions.

    Returns:
    - silo roster (same shape as list_silos)
    - compact health summary (or richer summary when include_audit=True)
    - recommended_actions derived from stale/index/error signals
    - ready_for_retrieval boolean to help agents gate first query
    """
    if not Path(_DB_PATH).is_dir():
        return {
            **_db_missing_error(),
            "silo_count": 0,
            "silos": [],
            "recommended_actions": ["Fix LLMLIBRARIAN_DB for this MCP process before retrieval."],
            "ready_for_retrieval": False,
        }

    from operations import op_list_silos

    silos_result = op_list_silos(_DB_PATH, check_staleness=check_staleness)
    silos = silos_result.get("silos", []) if isinstance(silos_result, dict) else []
    summary = _collect_health_summary(include_audit=include_audit)
    actions = _derive_recommended_actions(silos, summary)
    hnsw = summary.get("hnsw_consistency") or {}
    hnsw_desynced = int(hnsw.get("desynced_count", 0) or 0) if isinstance(hnsw, dict) else 0
    ready_for_retrieval = bool(
        summary.get("db_exists")
        and (summary.get("chroma_transport") != "http" or summary.get("chroma_server_ok", True))
        and hnsw_desynced == 0
    )

    return {
        **silos_result,
        "health_summary": summary,
        "recommended_actions": actions,
        "ready_for_retrieval": ready_for_retrieval,
    }


@mcp.tool()
def inspect_silo(silo: str, top: int = 50) -> dict:
    """
    Use when: diagnosing coverage inside one silo (file-level chunk distribution).
    Do not use when: you need content retrieval (`query_personal_knowledge`) or stale diagnostics (`session_context`/`health`).
    Pairs with: `query_personal_knowledge` and `repair_silo`.

    Show per-file chunk counts for a silo. Returns total chunks (registry vs ChromaDB),
    a registry_match flag, and a files list sorted by chunk count descending.
    Useful to diagnose zero-chunk files (likely failed to parse), detect duplicate content,
    or verify coverage after indexing. top= limits how many files are returned (default 50).
    """
    if not Path(_DB_PATH).is_dir():
        return _db_missing_error()
    from operations import op_inspect_silo
    try:
        with _mcp_chroma_lock("inspect_silo"):
            result = op_inspect_silo(_DB_PATH, silo, top=top)
        return result
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}"}
    finally:
        _release_chroma()


@mcp.tool()
def find_files(
    silos: list[str] | None = None,
    name_glob: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    date_field: str = "either",
    include_chunk_count: bool = False,
    limit: int = 50,
) -> dict:
    """
    Use when: the user asks for files by filename/date rather than document meaning.
    Do not use when: you need content-based answers (`query_personal_knowledge`/`multi_query_knowledge`).
    Pairs with: `query_personal_knowledge` after selecting a target file/silo.

    Find files by name and/or date against the manifest — no embeddings, no LLM.
    Use this for filename/date lookups like "today's journal entry" or "files from May 2026"
    where the user wants the *file*, not content discussion. Returns each hit with both
    name_date (parsed from filename) and mtime, plus a date_source field showing which
    one matched the filter ("name_date" | "mtime" | "both"). Filename precedence is the
    default because mtime is unreliable across syncs/restores. Set include_chunk_count=True
    to also report chunks per file (touches ChromaDB; otherwise this is metadata-only).
    Pass date_start / date_end as ISO strings (YYYY-MM-DD); same value for both means
    a single-day query. date_field accepts "name_date", "mtime", or "either" (default).
    """
    from datetime import date as _date

    from operations_find import op_find_files

    if not Path(_DB_PATH).is_dir():
        return _db_missing_error()

    def _parse(raw: str | None) -> _date | None:
        if not raw:
            return None
        return _date.fromisoformat(raw)

    try:
        lo = _parse(date_start)
        hi = _parse(date_end)
    except ValueError as e:
        return {"db_path": _DB_PATH, "error": f"invalid date: {e}"}

    if date_field not in ("name_date", "mtime", "either"):
        return {"db_path": _DB_PATH, "error": f"invalid date_field: {date_field}"}

    try:
        if include_chunk_count:
            with _mcp_chroma_lock("find_files"):
                result = op_find_files(
                    _DB_PATH,
                    silos=silos,
                    name_glob=name_glob,
                    date_start=lo,
                    date_end=hi,
                    date_field=date_field,  # type: ignore[arg-type]
                    include_chunk_count=True,
                    limit=int(limit),
                )
        else:
            result = op_find_files(
                _DB_PATH,
                silos=silos,
                name_glob=name_glob,
                date_start=lo,
                date_end=hi,
                date_field=date_field,  # type: ignore[arg-type]
                include_chunk_count=False,
                limit=int(limit),
            )
        return result
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}"}
    finally:
        if include_chunk_count:
            _release_chroma()


@mcp.tool()
def trigger_reindex(silo: str, confirm: bool = False) -> dict:
    """
    Use when: a registered silo is stale after source-file edits.
    Do not use when: immediately after `add_silo` or when fixing corruption (`repair_silo`).
    Pairs with: `session_context`/`list_silos` to validate staleness first.

    Re-index a registered silo from its source path in a background thread.
    Only works on already-registered silos — cannot add new paths.
    Requires confirm=True to proceed (safety guard against accidental calls).
    Returns immediately; the reindex runs in-process (uses the shared ChromaDB client,
    no concurrent-write crash risk). Concurrent calls are serialized via a lock.
    Check list_silos() for updated timestamp after completion.
    This updates indexed chunks from files; it is not `repair_silo` (no wipe) and not `watch_coverage` (not about pal bookmarks/daemons).

    IMPORTANT: Do NOT call immediately after add_silo. add_silo already runs its own
    full index in a background thread — a redundant trigger_reindex will queue a second
    reindex that can corrupt the HNSW index and cause lock contention.
    Only call when list_silos shows a stale timestamp AND source files have changed.
    """
    if not Path(_DB_PATH).is_dir():
        return {"status": "error", **_db_missing_error()}
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to start the reindex. This will re-crawl the silo's source folder.",
        }

    from state import list_silos as _list_silos, resolve_silo_to_slug

    slug = resolve_silo_to_slug(_DB_PATH, silo)
    if slug is None:
        return {"status": "error", "error": f"silo not found: {silo!r}"}

    all_silos = _list_silos(_DB_PATH)
    info = next((s for s in all_silos if s.get("slug") == slug), None)
    if not info:
        return {"status": "error", "error": f"silo registry entry missing for slug: {slug}"}

    source_path = info.get("path", "")
    if not source_path:
        return {"status": "error", "error": "silo has no registered source path"}

    if not Path(source_path).exists():
        return {"status": "error", "error": f"source path does not exist: {source_path}"}

    def _run_reindex() -> None:
        err: str | None = None
        _write_lock_held = False
        _mark_background_job_started(slug, kind="trigger_reindex", silo=slug, path=source_path)
        try:
            def _acquire_for_write() -> None:
                nonlocal _write_lock_held
                timeout = _mcp_lock_timeout_seconds()
                if not _chroma_lock.acquire(timeout=timeout):
                    raise TimeoutError(
                        f"Timed out after {timeout:g}s waiting for MCP Chroma lock during trigger_reindex. "
                        "A background reindex or another tool call is still using Chroma; retry or restart."
                    )
                _write_lock_held = True

            with _reindex_lock:
                from ingest import run_add

                # Drop the cached singleton PersistentClient before run_add
                # opens its own writer_client. Two live PersistentClients on
                # the same persist dir corrupt the HNSW segment writer
                # (chroma_client.py:148).
                _release_chroma()
                run_add(
                    path=source_path,
                    db_path=_DB_PATH,
                    incremental=True,
                    _pre_write_hook=_acquire_for_write,
                )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _logger.exception("trigger_reindex failed silo=%s path=%s", slug, source_path)
            traceback.print_exc(file=sys.stderr)
        finally:
            if _write_lock_held:
                _chroma_lock.release()
            finished = datetime.now(timezone.utc).isoformat()
            rec = {
                "silo": slug,
                "path": source_path,
                "finished_at": finished,
                "ok": err is None,
                **({"error": err} if err else {}),
            }
            _mark_background_job_finished(slug, rec)
            _release_chroma()

    t = threading.Thread(target=_run_reindex, daemon=True)
    t.start()
    return {
        "status": "started",
        "silo": slug,
        "display_name": info.get("display_name", slug),
        "path": source_path,
        "message": (
            "Reindex running in background thread (in-process, serialized). "
            "Call list_silos() after a few minutes to see the updated timestamp. "
            "Call health() for last_background_reindex status after completion."
        ),
    }


@mcp.tool()
def repair_silo(silo: str, confirm: bool = False) -> dict:
    """
    Use when: index corruption/zero-chunk inconsistencies are suspected.
    Do not use when: routine freshness updates are needed (`trigger_reindex`).
    Pairs with: `session_context`/`health` before and after repair.

    Hard-wipe and fully re-index a silo to fix ChromaDB index corruption or 0-chunk inconsistencies.
    Runs inside this process (safe even when the MCP server has the DB open).
    Equivalent to `llmli repair <silo>` but avoids the concurrent-write crash that happens
    when a second process opens the same ChromaDB path.
    Requires confirm=True to proceed (safety guard).
    This is synchronous — it blocks until complete (may take a few minutes for large silos).
    Unrelated to pal auto-watch: for bookmarks/daemon job coverage use `watch_coverage` (read-only).
    """
    if not Path(_DB_PATH).is_dir():
        return {"status": "error", **_db_missing_error()}
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to start the repair. This wipes and fully re-indexes the silo.",
        }

    from operations import op_repair_silo
    try:
        with _mcp_chroma_lock("repair_silo"):
            result = op_repair_silo(_DB_PATH, silo, verbose=False)
        if result.get("status") == "completed":
            result["message"] = (
                f"Repair complete. {result['files_indexed']} file(s) re-indexed, "
                f"{result['failures']} failure(s)."
            )
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        _release_chroma()


def _resolve_silo_under_path(silo: str, path: str) -> tuple[str | None, str | None, dict | None]:
    """Resolve silo slug and validate that path is inside the silo's registered source.

    Returns (slug, abs_path, error_dict). If error_dict is non-None, return it as the tool result.
    """
    from pathlib import Path as _Path
    from state import list_silos as _list_silos, resolve_silo_to_slug

    if not _Path(_DB_PATH).is_dir():
        return (None, None, {"status": "error", **_db_missing_error()})
    slug = resolve_silo_to_slug(_DB_PATH, silo)
    if slug is None:
        return (None, None, {"status": "error", "error": f"silo not found: {silo!r}"})
    info = next((s for s in _list_silos(_DB_PATH) if s.get("slug") == slug), None)
    if not info:
        return (None, None, {"status": "error", "error": f"silo registry entry missing for slug: {slug}"})
    silo_root = info.get("path") or ""
    if not silo_root:
        return (None, None, {"status": "error", "error": "silo has no registered source path"})
    abs_p = _Path(path).expanduser().resolve()
    silo_root_p = _Path(silo_root).resolve()
    try:
        abs_p.relative_to(silo_root_p)
    except ValueError:
        return (None, None, {
            "status": "error",
            "error": f"path is not under silo source: path={abs_p} silo_root={silo_root_p}",
        })
    return (slug, str(abs_p), None)


@mcp.tool()
def update_file(silo: str, path: str, confirm: bool = False) -> dict:
    """
    Use when: applying a single-file change inside an existing silo (watcher-style delta).
    Do not use when: onboarding/updating a full folder (`add_silo`) or fixing corruption (`repair_silo`).
    Pairs with: `remove_file` and `watch_coverage`.

    Re-index a single file in an already-registered silo. Used by `pal pull --watch`
    to push individual file changes through the shared Chroma client without
    spawning a separate writer process. Synchronous: blocks until the file is indexed.
    Path must resolve to a location under the silo's registered source folder.
    Requires confirm=True (safety guard, mirrors trigger_reindex / repair_silo).
    """
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to update the file.",
        }
    slug, abs_path, err = _resolve_silo_under_path(silo, path)
    if err:
        return err
    try:
        with _mcp_chroma_lock("update_file"):
            from ingest import update_single_file

            status, resolved = update_single_file(
                abs_path,
                db_path=_DB_PATH,
                silo_slug=slug,
                allow_cloud=True,  # path was already validated under the registered silo root
            )
        return {"status": status, "silo": slug, "path": resolved}
    except Exception as e:
        _logger.exception("update_file failed silo=%s path=%s", slug, abs_path)
        err_msg = f"{type(e).__name__}: {e}"
        from state import append_last_failures

        append_last_failures(_DB_PATH, [{"path": abs_path, "error": err_msg}])
        return {"status": "error", "error": err_msg}
    finally:
        _release_chroma()


@mcp.tool()
def remove_file(silo: str, path: str, confirm: bool = False) -> dict:
    """
    Use when: deleting one file from an already-registered silo.
    Do not use when: deleting/rebuilding an entire silo (`repair_silo` / remove+add flow).
    Pairs with: `update_file` and `watch_coverage`.

    Remove a single file's chunks and manifest entry from an already-registered silo.
    Used by `pal pull --watch` for delete events. Synchronous.
    Path must resolve to a location under the silo's registered source folder.
    Requires confirm=True (safety guard).
    """
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to remove the file.",
        }
    slug, abs_path, err = _resolve_silo_under_path(silo, path)
    if err:
        return err
    try:
        with _mcp_chroma_lock("remove_file"):
            from ingest import remove_single_file

            status, resolved = remove_single_file(
                abs_path,
                db_path=_DB_PATH,
                silo_slug=slug,
            )
        return {"status": status, "silo": slug, "path": resolved}
    except Exception as e:
        _logger.exception("remove_file failed silo=%s path=%s", slug, abs_path)
        err_msg = f"{type(e).__name__}: {e}"
        from state import append_last_failures

        append_last_failures(_DB_PATH, [{"path": abs_path, "error": err_msg}])
        return {"status": "error", "error": err_msg}
    finally:
        _release_chroma()


@mcp.tool()
def add_silo(
    path: str,
    silo: str | None = None,
    display_name: str | None = None,
    allow_cloud: bool = False,
    exclude_patterns: list[str] | None = None,
    full: bool = False,
    confirm: bool = True,
) -> dict:
    """
    Use when: indexing a new file/folder or refreshing a silo from its source path.
    Do not use when: you only need stale refresh after edits (`trigger_reindex`) or corruption repair (`repair_silo`).
    Pairs with: `session_context`/`list_silos` before and after indexing.

    Index a file or folder as a new silo (or update an existing one). Equivalent to `llmli add <path>`.
    silo: optional slug override (default: basename, slugified).
    display_name: optional human-readable name override.
    allow_cloud: set True to allow OneDrive/iCloud/Dropbox paths (blocked by default).
    full: set True to force a full non-incremental reindex (default: incremental).
    Returns immediately; indexing runs in a background thread (same process, serialized via lock).
    Call list_silos() or health() after a minute or two to confirm completion.

    IMPORTANT: Do NOT call trigger_reindex immediately after add_silo.
    add_silo already runs background indexing; a second immediate reindex can
    create lock contention and increase corruption risk.
    """
    from pathlib import Path as _Path

    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to start add_silo.",
        }

    p = _Path(path).resolve()
    if not p.exists():
        return {"status": "error", "error": f"path does not exist: {path}"}
    if not p.is_dir() and not p.is_file():
        return {"status": "error", "error": f"path must be a file or directory: {path}"}

    # Pre-derive a key for outcome tracking (best-guess slug; real slug written by thread)
    outcome_key = silo if silo else p.name

    def _run_add() -> None:
        err: str | None = None
        files_ok = 0
        n_failures = 0
        final_slug: str | None = None
        _write_lock_held = False
        _mark_background_job_started(outcome_key, kind="add_silo", path=str(p), silo=silo)
        try:
            def _acquire_for_write() -> None:
                nonlocal _write_lock_held
                timeout = _mcp_lock_timeout_seconds()
                if not _chroma_lock.acquire(timeout=timeout):
                    raise TimeoutError(
                        f"Timed out after {timeout:g}s waiting for MCP Chroma lock during add_silo. "
                        "A background reindex or another tool call is still using Chroma; retry or restart."
                    )
                _write_lock_held = True

            with _reindex_lock:
                from orchestration.ingest import IngestRequest, run_ingest
                from state import resolve_silo_by_path, resolve_silo_to_slug

                _release_chroma()
                result = run_ingest(
                    IngestRequest(
                        path=str(p),
                        db_path=_DB_PATH,
                        forced_silo_slug=silo,
                        display_name=display_name,
                        allow_cloud=allow_cloud,
                        incremental=not full,
                        exclude_patterns=exclude_patterns,
                        pre_write_hook=_acquire_for_write,
                    )
                )
                files_ok = result.files_indexed
                n_failures = result.failures
                final_slug = (
                    resolve_silo_to_slug(_DB_PATH, silo) if silo
                    else resolve_silo_by_path(_DB_PATH, p)
                )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _logger.exception("add_silo failed path=%s", p)
            traceback.print_exc(file=sys.stderr)
        finally:
            if _write_lock_held:
                _chroma_lock.release()
            finished = datetime.now(timezone.utc).isoformat()
            rec: dict = {
                "silo": final_slug or outcome_key,
                "path": str(p),
                "finished_at": finished,
                "ok": err is None,
                "files_indexed": files_ok,
                "failures": n_failures,
                **({"error": err} if err else {}),
            }
            _mark_background_job_finished(outcome_key, rec)
            _release_chroma()

    t = threading.Thread(target=_run_add, daemon=True)
    t.start()
    return {
        "status": "started",
        "path": str(p),
        "message": (
            "Indexing running in background thread (in-process, serialized). "
            "Call list_silos() after a minute or two to confirm the silo appears. "
            "Call health() to check last_background_reindex status after completion."
        ),
    }


@mcp.tool()
def mcp_runtime_status(verbose: bool = False) -> dict:
    """
    Use when: lock/process/runtime visibility is unclear for MCP or Chroma.
    Do not use when: you need content retrieval (`query_personal_knowledge`) or per-file coverage (`inspect_silo`).
    Pairs with: `session_context` for bootstrap and `health` for deep diagnostics.

    Returns a compact operator snapshot for agents:
    - mcp_http: pid lock visibility + mcp_server process multiplicity
    - chroma: transport and server reachability
    - jobs: active background jobs and last reindex outcomes
    - health_counts: query/ingest/HNSW counts
    - recommended_actions: short operational guidance
    """
    summary = _collect_health_summary(include_audit=False)
    mcp_lock = _read_mcp_pid_lock_snapshot()
    proc = _mcp_process_snapshot(verbose=verbose)

    mcp_http: dict = {**mcp_lock, **proc}
    chroma = {
        "transport": summary.get("chroma_transport"),
        "server_ok": summary.get("chroma_server_ok"),
        "host": summary.get("chroma_server_host"),
        "port": summary.get("chroma_server_port"),
    }
    jobs = _compact_runtime_jobs(summary, verbose=verbose)
    hnsw = summary.get("hnsw_consistency") or {}
    health_counts = {
        "query_error_count": int(((summary.get("query_health") or {}).get("recent_error_count", 0) or 0)),
        "ingest_failure_count": int(((summary.get("ingest_failures") or {}).get("last_failure_count", 0) or 0)),
        "hnsw_desynced_count": int((hnsw.get("desynced_count", 0) if isinstance(hnsw, dict) else 0) or 0),
    }

    actions = _derive_recommended_actions([], summary)
    if bool(mcp_http.get("multiple_mcp_processes")):
        actions.append("Multiple mcp_server.py processes detected; stop orphan MCP processes and keep one service instance.")
    if bool(mcp_http.get("lock_file_exists")) and mcp_http.get("lock_holder_pid") and not bool(mcp_http.get("lock_holder_alive")):
        actions.append("MCP PID lock file points to a dead process; restart MCP service to refresh lock state.")
    actions = _dedupe_lines(actions)

    out: dict = {
        "db_path": _DB_PATH,
        "db_exists": bool(summary.get("db_exists")),
        "mcp_http": mcp_http,
        "chroma": chroma,
        "jobs": jobs,
        "health_counts": health_counts,
        "recommended_actions": actions,
    }
    if verbose:
        out["summary_raw"] = summary
    return out


@mcp.tool()
def health() -> dict:
    """
    Use when: you need deep diagnostics (transport, query errors, ingest failures, HNSW, storage).
    Do not use when: you only need a routine session bootstrap (`session_context`).
    Pairs with: `session_context`, `repair_silo`, `trigger_reindex`.

    Diagnostic check. Returns db_path, db_exists, embedding model, Python version,
    and on-disk Chroma layout stats (including HNSW link_lists.bin bloat detection).
    Call this first if tools are failing, the disk is filling, or Python keeps spawning.
    """
    return _collect_health_summary(include_audit=True)


@mcp.tool()
def capabilities() -> str:
    """
    Use when: checking supported file types/extractors or smoke-testing MCP connectivity.
    Do not use when: diagnosing runtime/index health (`session_context`/`health`).
    Pairs with: `add_silo`.

    Return a plain-text report of all supported file types and extractors.
    No Ollama call required. Useful as a connectivity smoke test.
    """
    from ingest import get_capabilities_text
    return get_capabilities_text()


@mcp.resource("silos://list")
def resource_silos() -> str:
    """Read-only JSON snapshot of all registered silos. May be stale — call list_silos tool for live data."""
    import json
    if not Path(_DB_PATH).is_dir():
        return json.dumps({**_db_missing_error(), "silos": []}, indent=2)
    from state import list_silos as _list_silos
    return json.dumps({
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "silos": _list_silos(_DB_PATH),
    }, indent=2)


@mcp.resource("silos://{slug}")
def get_silo(slug: str) -> str:
    """
    Fetch live silo metadata by slug (e.g. silos://much-thinks, silos://hot_seat).
    Returns JSON with silo details: slug, display_name, path, file_count, chunk_count,
    last_indexed, and doc_type_breakdown. Resolved dynamically at request time.
    """
    import json
    if not Path(_DB_PATH).is_dir():
        return json.dumps(_db_missing_error(), indent=2)
    from state import list_silos as _list_silos
    all_silos = _list_silos(_DB_PATH)
    silo_info = next((s for s in all_silos if s.get("slug") == slug), None)
    if silo_info is None:
        raise ValueError(f"silo not found: {slug}")
    return json.dumps(silo_info, indent=2)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> Response:
    """Simple liveness check for process supervisors and reverse proxies."""
    return JSONResponse({
        "ok": True,
        "service": "llmLibrarian-mcp",
        "version": _package_version(),
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        "started_at": _SERVER_STARTED_AT,
    })


if __name__ == "__main__":
    transport = os.environ.get("LLMLIBRARIAN_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"stdio", "http", "sse", "streamable-http"}:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_TRANSPORT must be one of: "
            "stdio, http, sse, streamable-http"
        )

    # Acquire single-instance lock for persistent HTTP server processes.
    # Stdio processes are ephemeral (one per Claude Desktop conversation) and
    # must not acquire this lock — they'd block each other needlessly.
    if transport != "stdio":
        _acquire_server_lock()
        _SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat()

    auth_provider = _auth_for_transport(transport)
    if auth_provider is not None:
        mcp.auth = auth_provider

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        host = os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("LLMLIBRARIAN_MCP_PORT", "8765"))
        path = os.environ.get("LLMLIBRARIAN_MCP_PATH", "/mcp")
        log_level = os.environ.get("LLMLIBRARIAN_MCP_LOG_LEVEL", "warning")
        stateless_http = _env_bool("LLMLIBRARIAN_MCP_STATELESS_HTTP", True)
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            path=path,
            log_level=log_level,
            stateless_http=stateless_http,
        )
