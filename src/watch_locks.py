"""
Reading ``pal pull --watch`` lock files.

``pal`` writes one lock file per watched silo under ``$PAL_HOME/watch_locks``.
Two very different consumers read them:

- ``pal`` itself, to report watcher status and to avoid double-starting a watcher.
- ``chroma_client.preflight_embedded_write``, to refuse an embedded write while a
  watcher holds the index open.

Both must agree on the on-disk format -- including the legacy bare-integer form,
where the file holds only a pid. A reader that understands JSON but not the
legacy form silently treats a live watcher as absent, which is the failure mode
this module exists to prevent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_locks_dir() -> Path:
    """Resolve ``$PAL_HOME/watch_locks`` at call time."""
    pal_home = Path(os.environ.get("PAL_HOME", str(Path.home() / ".pal"))).expanduser()
    return pal_home / "watch_locks"


def iter_watch_locks(locks_dir: Path | None = None) -> list[Path]:
    d = default_locks_dir() if locks_dir is None else locks_dir
    if not d.is_dir():
        return []
    return sorted(d.glob("*.pid"))


def read_watch_lock(lock_path: Path) -> dict | None:
    """Parse one lock file into a dict, or None if unreadable/empty.

    Current format is a JSON object. The legacy format is a bare integer pid,
    normalized here to ``{"pid": <int>}``.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        return {"pid": int(raw)}
    except Exception:
        return None


def read_watch_lock_pid(lock_path: Path) -> int | None:
    data = read_watch_lock(lock_path)
    if not data:
        return None
    try:
        return int(data.get("pid"))
    except Exception:
        return None


def active_watchers_for_db(db_path: str, locks_dir: Path | None = None) -> list[str]:
    """Human-readable labels for live watcher processes holding ``db_path``.

    A lock that records no ``db_path`` is counted -- legacy locks predate that
    field, and assuming they belong to some other database is the unsafe guess.
    """
    from constants import pid_is_running

    db_resolved = str(Path(db_path).expanduser().resolve())
    active: list[str] = []
    for lock_path in iter_watch_locks(locks_dir):
        data = read_watch_lock(lock_path)
        if not data:
            continue
        lock_db = str(data.get("db_path") or "").strip()
        if lock_db and str(Path(lock_db).expanduser().resolve()) != db_resolved:
            continue
        pid = read_watch_lock_pid(lock_path)
        if pid is None or not pid_is_running(pid):
            continue
        silo = str(data.get("silo") or lock_path.stem)
        active.append(f"pal pull --watch (silo={silo}, pid={pid})")
    return active
