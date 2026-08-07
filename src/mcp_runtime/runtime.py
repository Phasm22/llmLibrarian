"""Single-instance enforcement, auth, and process visibility for the MCP server.

Two MCP servers on one embedded ChromaDB path corrupt the HNSW index, so HTTP
transports take an exclusive flock on a PID file at startup. Stdio processes do
not: a host spawns one per conversation and they would block each other for no
benefit. That asymmetry is why ``process_snapshot()`` exists -- under stdio the
lock cannot tell you how many servers are live, so we scan for them instead.
"""

from __future__ import annotations

import atexit
import errno
import os
import signal
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from constants import env_flag, pid_is_running

_server_lock_fd: int | None = None
_server_lock_path: Path | None = None


def package_version_string() -> str:
    try:
        return package_version("llmlibrarian")
    except PackageNotFoundError:
        return "unknown"


def pid_file_path() -> Path:
    # Fixed path, deliberately not under XDG_RUNTIME_DIR, so shell-launched and
    # service-launched processes always agree on the same lock file.
    uid = os.getuid() if hasattr(os, "getuid") else "0"
    return Path(f"/tmp/llmlibrarian-mcp-{uid}.pid")


def release_server_lock() -> None:
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
    release_server_lock()
    sys.exit(0)


def acquire_server_lock() -> None:
    """Take the single-instance lock; exit(0) cleanly if another server holds it."""
    global _server_lock_fd, _server_lock_path
    try:
        import fcntl
    except ImportError:
        return  # Windows — fcntl is Unix-only

    pid_path = pid_file_path()
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

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _server_lock_fd = fd
    _server_lock_path = pid_path
    atexit.register(release_server_lock)
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)


def pid_lock_snapshot() -> dict:
    """PID-lock visibility for mcp_runtime_status. Never raises."""
    path = pid_file_path()
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
            out["lock_holder_alive"] = pid_is_running(pid)
        return out
    except Exception as e:
        out["introspection_error"] = f"{type(e).__name__}: {e}"
        return out


def process_snapshot(*, verbose: bool = False) -> dict:
    """Count live mcp_server.py processes by scanning /proc.

    This is the only way to see stdio servers, which take no PID lock. Falls
    back gracefully where procfs is unavailable.
    """
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
            try:
                raw = (entry / "cmdline").read_bytes()
            except Exception:
                continue
            if not raw:
                continue
            parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
            joined = " ".join(parts)
            if "mcp_server.py" in joined and "llmLibrarian" in joined:
                row: dict = {"pid": int(entry.name)}
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

VALID_TRANSPORTS = {"stdio", "http", "sse", "streamable-http"}


def resolve_transport() -> str:
    transport = os.environ.get("LLMLIBRARIAN_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_TRANSPORT must be one of: "
            + ", ".join(sorted(VALID_TRANSPORTS))
        )
    return transport


def auth_for_transport(transport: str):
    """Bearer auth provider for HTTP transports, or None.

    stdio is exempt: the host launched this process directly, so there is no
    network boundary to authenticate across.
    """
    if transport == "stdio":
        return None
    if not env_flag("LLMLIBRARIAN_MCP_REQUIRE_AUTH", False):
        return None
    token = os.environ.get("LLMLIBRARIAN_MCP_AUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_AUTH_TOKEN is required when "
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH=true."
        )
    from fastmcp.server.auth.auth import AuthProvider
    from mcp.server.auth.provider import AccessToken

    class StaticBearerTokenAuth(AuthProvider):
        """Static bearer token verifier for MCP HTTP transports."""

        def __init__(self, expected: str):
            super().__init__()
            self._token = expected.strip()

        async def verify_token(self, candidate: str) -> AccessToken | None:
            import hmac

            if not hmac.compare_digest(candidate, self._token):
                return None
            return AccessToken(
                token=candidate,
                client_id="llmli-mcp-client",
                scopes=["mcp"],
            )

    return StaticBearerTokenAuth(token)


def is_loopback_bind() -> bool:
    """True when the server binds only to the local host."""
    host = os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    return host in {"127.0.0.1", "::1", "localhost"}
