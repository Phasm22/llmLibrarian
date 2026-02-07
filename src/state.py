"""
Silo registry and last-failures for llmli add/ls/log. Stored next to DB path.
Display name = original folder name; slug = canonical key (lowercase, hyphens).
"""
import json
import re
from pathlib import Path
from typing import Any


def slugify(name: str) -> str:
    """Canonical silo id: lowercase, spaces/special -> hyphens, collapse."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s or "default"

def _registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_registry.json"
    return p.parent / "llmli_registry.json"

def _failures_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_last_failures.json"
    return p.parent / "llmli_last_failures.json"

def _read_registry(db_path: str | Path) -> dict[str, Any]:
    path = _registry_path(db_path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _write_registry(db_path: str | Path, data: dict[str, Any]) -> None:
    path = _registry_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def update_silo(
    db_path: str | Path,
    slug: str,
    folder_path: str,
    files_indexed: int,
    chunks_count: int,
    updated_iso: str,
    display_name: str | None = None,
) -> None:
    """Record silo after add. Uses slug as key; display_name = original folder name."""
    reg = _read_registry(db_path)
    reg[slug] = {
        "slug": slug,
        "display_name": display_name or slug,
        "path": folder_path,
        "files_indexed": files_indexed,
        "chunks_count": chunks_count,
        "updated": updated_iso,
    }
    _write_registry(db_path, reg)

def list_silos(db_path: str | Path) -> list[dict[str, Any]]:
    """Return list of silo dicts (slug, display_name, path, files_indexed, chunks_count, updated)."""
    reg = _read_registry(db_path)
    out = []
    for v in reg.values():
        v = dict(v)
        v.setdefault("display_name", v.get("slug", ""))
        out.append(v)
    return out

def resolve_silo_to_slug(db_path: str | Path, name_or_slug: str) -> str | None:
    """Return slug for the given silo name or slug. None if not found."""
    reg = _read_registry(db_path)
    if name_or_slug in reg:
        return name_or_slug
    for slug, data in reg.items():
        if (data.get("display_name") or slug) == name_or_slug:
            return slug
    return None


def remove_silo(db_path: str | Path, name_or_slug: str) -> str | None:
    """Remove silo from registry by slug or display_name. Returns slug removed, or None if not found."""
    slug = resolve_silo_to_slug(db_path, name_or_slug)
    if slug is None:
        return None
    reg = _read_registry(db_path)
    del reg[slug]
    _write_registry(db_path, reg)
    return slug

def set_last_failures(db_path: str | Path, failures: list[dict[str, str]]) -> None:
    """Save last add failures for 'log --last'."""
    path = _failures_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(failures, f, indent=2)

def get_last_failures(db_path: str | Path) -> list[dict[str, str]]:
    """Load last add failures."""
    path = _failures_path(db_path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
