"""Pal bookmark registry JSON read/write (path supplied by caller)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_pal_registry(registry_path: Path) -> dict[str, Any]:
    """Read the pal registry. Migrates legacy 'sources' key to 'bookmarks' transparently."""
    empty: dict[str, Any] = {"bookmarks": []}
    if not registry_path.exists():
        return empty
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return empty
    if "sources" in data and "bookmarks" not in data:
        raw = data.get("sources") or []
        data["bookmarks"] = [
            {k: v for k, v in entry.items() if k in ("path", "name", "silo")}
            for entry in raw
            if isinstance(entry, dict) and entry.get("path")
        ]
        del data["sources"]
    return data


def write_pal_registry(registry_path: Path, data: dict[str, Any]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
