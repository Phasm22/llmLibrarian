"""
Silo registry and last-failures for llmli add/ls/log. Stored next to DB path.
Display name = original folder name; slug = canonical key (lowercase, hyphens).
"""
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


def slugify(name: str, path: str | None = None) -> str:
    """Canonical silo id: lowercase, spaces/special -> hyphens, collapse + hash suffix."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    prefix = s or "default"
    material = f"{name or ''}|{path or ''}"
    h = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{h}"

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
    except Exception as e:
        print(f"[llmli] registry read failed: {path}: {e}; using empty registry.", file=sys.stderr)
        return {}

def _write_registry(db_path: str | Path, data: dict[str, Any]) -> None:
    path = _registry_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[llmli] registry write failed: {path}: {e}", file=sys.stderr)
        raise

def update_silo(
    db_path: str | Path,
    slug: str,
    folder_path: str,
    files_indexed: int,
    chunks_count: int,
    updated_iso: str,
    display_name: str | None = None,
    language_stats: dict | None = None,
) -> None:
    """Record silo after add. Preserves unknown keys (e.g. prompt overrides)."""
    reg = _read_registry(db_path)
    existing = reg.get(slug)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry.update(
        {
            "slug": slug,
            "display_name": display_name or slug,
            "path": folder_path,
            "files_indexed": files_indexed,
            "chunks_count": chunks_count,
            "updated": updated_iso,
        }
    )
    if language_stats is not None:
        entry["language_stats"] = language_stats
    reg[slug] = entry
    _write_registry(db_path, reg)


def set_silo_prompt_override(db_path: str | Path, slug: str, prompt: str | None) -> bool:
    """Set or clear per-silo prompt override. Returns False when silo is missing."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return False
    if prompt is None:
        entry.pop("prompt_override", None)
    else:
        entry["prompt_override"] = prompt
    reg[slug] = entry
    _write_registry(db_path, reg)
    return True


def get_silo_prompt_override(db_path: str | Path, slug: str) -> str | None:
    """Get prompt override for a silo if present."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return None
    value = entry.get("prompt_override")
    return value if isinstance(value, str) else None


def get_silo_display_name(db_path: str | Path, slug: str) -> str | None:
    """Get display name for a silo by slug."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return None
    value = entry.get("display_name")
    return value if isinstance(value, str) else None

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


def resolve_silo_prefix(db_path: str | Path, prefix: str) -> str | None:
    """Return slug if prefix uniquely matches a registered slug."""
    reg = _read_registry(db_path)
    matches = [slug for slug in reg.keys() if slug.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_silo_by_path(db_path: str | Path, path: str | Path) -> str | None:
    """Return slug for the given exact path, if registered."""
    reg = _read_registry(db_path)
    p = str(Path(path).resolve())
    for slug, data in reg.items():
        if (data.get("path") or "") == p:
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


def remove_manifest_silo(db_path: str | Path, slug: str) -> None:
    """Remove silo from file manifest (if present)."""
    from ingest import _file_manifest_path  # lazy to avoid import cycle
    path = _file_manifest_path(db_path)
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        silos = data.get("silos") or {}
        if slug in silos:
            del silos[slug]
            data["silos"] = silos
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception:
        return

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
