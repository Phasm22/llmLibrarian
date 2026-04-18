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


def cleanup_stale_registry_entries(llmli_registry_path: Path) -> bool:
    """Remove old-format silos (no hash suffix) when newer ones (with hash) exist for the same path."""
    if not llmli_registry_path.exists():
        return False
    try:
        with open(llmli_registry_path, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        return False

    # Build a map of source paths → slugs
    path_to_slugs: dict[str, list[str]] = {}
    for slug, entry in reg.items():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if path:
            path_to_slugs.setdefault(str(path), []).append(slug)

    # For each path with multiple slugs, keep the longest one (newest format with hash)
    to_delete = set()
    for path, slugs in path_to_slugs.items():
        if len(slugs) > 1:
            longest = max(slugs, key=len)
            for slug in slugs:
                if slug != longest:
                    to_delete.add(slug)

    if not to_delete:
        return False

    for slug in to_delete:
        del reg[slug]

    with open(llmli_registry_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
    return True


def write_pal_registry(registry_path: Path, data: dict[str, Any]) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
