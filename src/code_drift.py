"""Notice when the code on disk changed under a long-running MCP process.

The macOS launchers exec this checkout's working tree, and tools import their
modules lazily. A process that started on one commit and lazily imports a module
from a later one runs a mixed module graph. On 2026-10-02 a Claude Desktop stdio
server started 09-28 had cached an old file_registry, then imported a newer
operations_find that needed read_visible_manifest, and every find_files raised
ImportError. The fix for that process is a restart; this makes the cause visible.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def git_head(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    head = (out.stdout or "").strip()
    return head or None


def newest_source_mtime(root: Path) -> float:
    newest = 0.0
    candidates = [root / "mcp_server.py"]
    src = root / "src"
    if src.is_dir():
        candidates.extend(src.rglob("*.py"))
    for path in candidates:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def snapshot(root: Path) -> dict[str, Any]:
    return {"head": git_head(root), "newest_source_mtime": newest_source_mtime(root)}


def drift(start: dict[str, Any], root: Path) -> dict[str, Any]:
    now = snapshot(root)
    head_changed = bool(start.get("head") and now["head"] and start["head"] != now["head"])
    files_changed = now["newest_source_mtime"] > float(start.get("newest_source_mtime") or 0)
    out: dict[str, Any] = {"changed_since_start": head_changed or files_changed}
    if head_changed:
        out["started_head"] = (start.get("head") or "")[:12]
        out["current_head"] = (now["head"] or "")[:12]
    if files_changed and not head_changed:
        out["source_files_modified"] = True
    return out


def restart_hint(*, transport: str, pid: int, started_at: str | None) -> str:
    how = (
        "restart the MCP client that spawned it (or reconnect the server in the client)"
        if transport == "stdio"
        else "run `pal mcp stop && pal mcp start` (or let launchd restart it)"
    )
    since = f", started {started_at}" if started_at else ""
    return (
        f"The code on disk changed after this MCP server started (pid {pid}{since}); "
        f"it is running a mix of old and new modules. To load the new code, {how}."
    )
