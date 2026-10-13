"""Who asked: identify the MCP client behind each audited query.

The query audit records *what* was asked and *which files* answered. This
module supplies the third column — the caller — from whatever the transport
exposes:

- stdio: one server process per client, so identity is fixed at startup —
  the parent application (Claude, claude, Cursor) and the Claude Code session
  env the client passes down (``CLAUDE_CODE_SESSION_ID`` ties a query to its
  transcript). ``clientInfo`` from the MCP handshake arrives per session.
- HTTP (stateless): every POST is a fresh MCP session, so ``clientInfo`` only
  rides on the ``initialize`` request. On loopback the peer TCP port maps to
  the owning process via ``lsof``, which names the app (AnythingLLM) and keys
  the ``clientInfo`` remembered from its handshake.

Everything here is best-effort: a lookup failure yields ``None``, never an
error in the tool call it is attributing.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

# Processes that sit between a client app and the server it spawns.
_WRAPPERS = {
    "disclaimer", "uv", "sh", "bash", "zsh", "fish", "login", "env",
    "python", "python3", "Python", "node",
}

# Non-secret Claude Code env vars a stdio child inherits. Whitelist only:
# the same env carries OAuth tokens, account UUIDs and the user's email.
_ENV_FIELDS = {
    "CLAUDE_CODE_SESSION_ID": "session",
    "CLAUDE_CODE_ENTRYPOINT": "entrypoint",
    "AI_AGENT": "agent",
}

# MCP clientInfo.name -> display label.
_FRIENDLY_NAMES = {
    "claude-code": "Claude Code",
    "claude-ai": "Claude Desktop",
    "cursor": "Cursor",
    "cursor-vscode": "Cursor",
    "visual studio code": "VS Code",
    "codex": "Codex",
    "openwebui": "Open WebUI",
}

_PEER_TTL_SECONDS = 60.0
_LOOKUP_TIMEOUT_SECONDS = 2.0


def _app_from_command(command: str) -> str:
    """``/Applications/X.app/Contents/.../Y Helper.app/...`` -> ``X``; else the basename."""
    for part in Path(command).parts:
        if part.endswith(".app"):
            return part[: -len(".app")]
    return Path(command).name


def _ps_parent_and_command(pid: int) -> tuple[int, str] | None:
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_LOOKUP_TIMEOUT_SECONDS,
        ).stdout.strip()
        ppid_str, _, command = out.partition(" ")
        return int(ppid_str), command.strip()
    except Exception:
        return None


def app_for_pid(pid: int, *, include_self: bool = True) -> str | None:
    """First real application at or above ``pid``, skipping wrapper processes."""
    for _ in range(6):
        if pid <= 1:
            return None
        row = _ps_parent_and_command(pid)
        if row is None:
            return None
        ppid, command = row
        name = Path(command).name
        if include_self and name and name not in _WRAPPERS:
            return _app_from_command(command)
        include_self = True
        pid = ppid
    return None


def client_app_name() -> str | None:
    """Best-effort name of the MCP client that spawned this stdio server.

    Walks up the parent chain past wrapper processes (uv, Claude's
    disclaimer helper, shells) to the first real application, so multiple
    stdio instances are distinguishable in ps (e.g. Claude vs Cursor).
    """
    return app_for_pid(os.getppid())


def static_stdio_identity() -> dict:
    """Identity of a stdio server's one client; fixed for the process lifetime."""
    identity: dict = {"transport": "stdio", "server_pid": os.getpid()}
    app = client_app_name()
    if app:
        identity["app"] = app
    for env_name, field in _ENV_FIELDS.items():
        value = (os.environ.get(env_name) or "").strip()
        if value:
            identity[field] = value[:200]
    return identity


def parse_lsof_pids(output: str, *, exclude_pid: int) -> list[int]:
    """PIDs from ``lsof -F p`` output (``p<pid>`` lines), minus ``exclude_pid``."""
    pids: list[int] = []
    for line in output.splitlines():
        if not line.startswith("p"):
            continue
        try:
            pid = int(line[1:])
        except ValueError:
            continue
        if pid != exclude_pid and pid not in pids:
            pids.append(pid)
    return pids


_peer_cache: dict[int, tuple[float, dict | None]] = {}
_peer_lock = threading.Lock()


def peer_for_port(port: int) -> dict | None:
    """``{pid, app}`` of the local process on the far end of a loopback connection.

    ``port`` is the client's ephemeral port. Cached per port so a keep-alive
    connection costs one ``lsof`` (~50 ms).
    """
    now = time.monotonic()
    with _peer_lock:
        hit = _peer_cache.get(port)
        if hit and now - hit[0] < _PEER_TTL_SECONDS:
            return hit[1]
    peer: dict | None = None
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fp"],
            capture_output=True, text=True, timeout=_LOOKUP_TIMEOUT_SECONDS,
        ).stdout
        pids = parse_lsof_pids(out, exclude_pid=os.getpid())
        if pids:
            peer = {"pid": pids[0]}
            app = app_for_pid(pids[0])
            if app:
                peer["app"] = app
    except Exception:
        peer = None
    with _peer_lock:
        if len(_peer_cache) > 512:
            _peer_cache.clear()
        _peer_cache[port] = (now, peer)
    return peer


# clientInfo seen on an HTTP initialize, keyed by the peer PID that sent it,
# so later stateless tool calls from the same process inherit name/version.
_handshakes: dict[int, dict] = {}


def remember_handshake(pid: int | None, info: dict) -> None:
    if pid and info:
        with _peer_lock:
            _handshakes[pid] = info


def handshake_for(pid: int | None) -> dict:
    if not pid:
        return {}
    with _peer_lock:
        return dict(_handshakes.get(pid) or {})


def client_info_dict(client_info) -> dict:
    """``{client_name, client_version}`` from an MCP Implementation object."""
    if client_info is None:
        return {}
    out = {}
    name = getattr(client_info, "name", None)
    version = getattr(client_info, "version", None)
    if name:
        out["client_name"] = str(name)[:100]
    if version:
        out["client_version"] = str(version)[:50]
    return out


def client_label(client: dict) -> str:
    """Human label: explicit tag > known clientInfo name > app > UA product > transport."""
    tag = (client.get("tag") or "").strip()
    if tag:
        return tag
    name = (client.get("client_name") or "").strip()
    if name:
        lowered = name.lower()
        if lowered in _FRIENDLY_NAMES:
            return _FRIENDLY_NAMES[lowered]
        if lowered.startswith("anythingllm") or lowered.startswith("anything-llm"):
            return "AnythingLLM"
    app = (client.get("app") or "").strip()
    if app:
        return "Claude Code" if app == "claude" else app
    if name:
        return name
    agent = (client.get("user_agent") or "").strip()
    if agent:
        return agent.split("/", 1)[0].split(" ", 1)[0]
    return client.get("transport") or "unknown"
