"""
Silo registry and last-failures for llmli add/ls/log. Stored next to DB path.
Display name = original folder name; slug = canonical key (lowercase, hyphens).
"""
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
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

def canonical_folder_path(path: str | Path) -> str:
    """Canonical on-disk identity for a silo root.

    resolve() follows symlinks, so ~/llmLibrarian and the Developer/active checkout
    it points at collapse to one path — and therefore one slug. Registering both
    spellings used to produce two silos over identical files, which double-counted
    chunks and split retrieval across duplicates.
    """
    return str(Path(path).expanduser().resolve())


def current_host() -> str:
    """Short hostname, used to stamp which machine indexed a silo."""
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return ""


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

def _query_health_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_query_health.json"
    return p.parent / "llmli_query_health.json"

def registry_transaction(db_path: str | Path):
    """Exclusive lock over this DB's silo registry, for read-modify-write.

    Every mutator below holds this across BOTH the read and the write. Locking
    only the write still loses updates: two processes read the same dict, each
    edits its own copy, and the second write erases the first one's change.
    Reentrant, so a mutator may call another one.
    """
    from registry_lock import registry_transaction as _txn

    return _txn(_registry_path(db_path))


def _read_registry(db_path: str | Path) -> dict[str, Any]:
    path = _registry_path(db_path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        # File exists but cannot be opened (e.g. EMFILE / EACCES). Treating this
        # as an empty registry causes silent "silo not found" errors that mask
        # the real problem. Surface it so callers (and the watcher retry loop)
        # stop hammering.
        raise RuntimeError(f"registry read failed: {path}: {e}") from e
    except json.JSONDecodeError as e:
        print(f"[llmli] registry corrupt: {path}: {e}; using empty registry.", file=sys.stderr)
        return {}

def _write_registry(db_path: str | Path, data: dict[str, Any]) -> None:
    path = _registry_path(db_path)
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A fixed "<name>.tmp" is not safe here: several writers share this DB
        # (one watcher per silo, the MCP server, the CLI). Two of them would use
        # the same scratch path, and the loser's os.replace raised FileNotFoundError
        # after the winner had already moved it away. Give each writer its own.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except Exception as e:
        print(f"[llmli] registry write failed: {path}: {e}", file=sys.stderr)
        raise
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass

def update_silo(
    db_path: str | Path,
    slug: str,
    folder_path: str,
    files_indexed: int,
    chunks_count: int,
    updated_iso: str,
    display_name: str | None = None,
    language_stats: dict | None = None,
    image_vision_enabled: bool | None = None,
    exclude_patterns: list[str] | None = None,
) -> None:
    """Record silo after add. Preserves unknown keys (e.g. prompt overrides)."""
    with registry_transaction(db_path):
        reg = _read_registry(db_path)
        existing = reg.get(slug)
        entry = dict(existing) if isinstance(existing, dict) else {}
        entry.update(
            {
                "slug": slug,
                "display_name": display_name or slug,
                "path": canonical_folder_path(folder_path),
                "files_indexed": files_indexed,
                "chunks_count": chunks_count,
                "updated": updated_iso,
                # Silos are machine-local: the Mac and the Linux PC keep separate
                # registries over separate filesystems. Stamping the host means a
                # roster copied or quoted out of context still says where it came from.
                "host": current_host(),
            }
        )
        if language_stats is not None:
            entry["language_stats"] = language_stats
        if image_vision_enabled is not None:
            entry["image_vision_enabled"] = bool(image_vision_enabled)
        if exclude_patterns is not None:
            cleaned = [str(p).strip() for p in exclude_patterns if str(p).strip()]
            entry["exclude_patterns"] = cleaned
        reg[slug] = entry
        _write_registry(db_path, reg)


def set_silo_prompt_override(db_path: str | Path, slug: str, prompt: str | None) -> bool:
    """Set or clear per-silo prompt override. Returns False when silo is missing."""
    with registry_transaction(db_path):
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


def get_silo_image_vision_enabled(db_path: str | Path, slug: str) -> bool | None:
    """Get persisted image-vision setting for a silo, if present."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return None
    value = entry.get("image_vision_enabled")
    if isinstance(value, bool):
        return value
    return None

def get_silo_exclude_patterns(db_path: str | Path, slug: str) -> list[str]:
    """Get persisted per-silo exclude patterns, if present."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return []
    value = entry.get("exclude_patterns")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        pattern = str(raw).strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        out.append(pattern)
    return out


def get_silo_artifact_compile(db_path: str | Path, slug: str) -> dict[str, Any] | None:
    """Get artifact compile metadata for a silo, if present."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    if not isinstance(entry, dict):
        return None
    value = entry.get("last_artifact_compile")
    return dict(value) if isinstance(value, dict) else None


def set_silo_artifact_compile(
    db_path: str | Path,
    slug: str,
    payload: dict[str, Any] | None,
) -> bool:
    """Set or clear artifact compile metadata for a silo."""
    with registry_transaction(db_path):
        reg = _read_registry(db_path)
        entry = reg.get(slug)
        if not isinstance(entry, dict):
            return False
        if payload is None:
            entry.pop("last_artifact_compile", None)
        else:
            entry["last_artifact_compile"] = dict(payload)
        reg[slug] = entry
        _write_registry(db_path, reg)
        return True

def set_silo_private(db_path: str | Path, slug: str, private: bool) -> bool:
    """Mark a silo private, or clear the mark. Returns False when silo is missing.

    Private silos are excluded from unscoped retrieval and are only reachable
    when the caller names them with an explicit silo=. See private_silo_slugs().
    """
    with registry_transaction(db_path):
        reg = _read_registry(db_path)
        entry = reg.get(slug)
        if not isinstance(entry, dict):
            return False
        if private:
            entry["private"] = True
        else:
            entry.pop("private", None)
        reg[slug] = entry
        _write_registry(db_path, reg)
        return True


def is_silo_private(db_path: str | Path, slug: str) -> bool:
    """True when the silo carries the private flag."""
    reg = _read_registry(db_path)
    entry = reg.get(slug)
    return bool(entry.get("private")) if isinstance(entry, dict) else False


def private_silo_slugs(db_path: str | Path) -> list[str]:
    """Slugs that must not appear in unscoped retrieval.

    Enforcement lives here rather than in caller discipline on purpose: an
    unscoped multi-query returned tax and medical chunks nobody asked for, and
    "remember not to" is not a control.
    """
    try:
        reg = _read_registry(db_path)
    except Exception:
        # Fail closed is not an option — we cannot name the private silos, so we
        # cannot filter them. Callers treat the empty list as "unknown"; see
        # private_filter_clause(), which raises rather than querying blind.
        raise
    return sorted(slug for slug, entry in reg.items() if isinstance(entry, dict) and entry.get("private"))


def private_filter_clause(db_path: str | Path) -> dict[str, Any] | None:
    """Chroma where-clause that excludes every private silo, or None if there are none.

    Raises if the registry cannot be read: without a readable registry we cannot
    know which silos are private, and an unfiltered query is exactly the leak
    this guards. Callers surface the error instead of returning chunks.
    """
    slugs = private_silo_slugs(db_path)
    if not slugs:
        return None
    return {"silo": {"$nin": slugs}}


def list_silos(db_path: str | Path) -> list[dict[str, Any]]:
    """Return list of silo dicts (slug, display_name, path, files_indexed, chunks_count, updated)."""
    reg = _read_registry(db_path)
    out = []
    for v in reg.values():
        v = dict(v)
        v.setdefault("display_name", v.get("slug", ""))
        v["private"] = bool(v.get("private"))
        v.setdefault("host", "")
        out.append(v)
    return out

def list_visible_silos(db_path: str | Path) -> list[dict[str, Any]]:
    """Silos eligible for unscoped retrieval — private ones removed.

    Use this anywhere a query picks a silo on the user's behalf (scope binding,
    catalog ranking, cross-silo counts). list_silos() stays the full roster for
    status and diagnostics: knowing a private corpus exists is allowed, pulling
    from it unasked is not.
    """
    return [s for s in list_silos(db_path) if not s.get("private")]


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
    p = canonical_folder_path(path)
    for slug, data in reg.items():
        if canonical_folder_path(data.get("path") or "/nonexistent") == p:
            return slug
    return None


def remove_silo(db_path: str | Path, name_or_slug: str) -> str | None:
    """Remove silo from registry by slug or display_name. Returns slug removed, or None if not found."""
    with registry_transaction(db_path):
        slug = resolve_silo_to_slug(db_path, name_or_slug)
        if slug is None:
            return None
        reg = _read_registry(db_path)
        del reg[slug]
        _write_registry(db_path, reg)
        return slug


def remove_manifest_silo(db_path: str | Path, slug: str) -> None:
    """Remove silo from file manifest (if present).

    Routed through ``_update_file_manifest`` rather than editing the file
    directly: that helper holds the manifest lock across the read and the write
    and replaces the file atomically. The previous version did neither — it
    truncated the manifest with ``open(path, "w")`` and rebuilt it in place, so
    a crash mid-write left an empty manifest and a concurrent writer's changes
    were silently dropped.
    """
    from file_registry import _update_file_manifest  # lazy to avoid import cycle

    def _drop(manifest: dict) -> None:
        silos = manifest.get("silos") or {}
        if slug in silos:
            del silos[slug]
            manifest["silos"] = silos

    try:
        _update_file_manifest(db_path, _drop)
    except Exception:
        return

def failures_path(db_path: str | Path) -> Path:
    """Absolute path to llmli_last_failures.json for the given DB."""
    return _failures_path(db_path)


def set_last_failures(db_path: str | Path, failures: list[dict[str, str]]) -> None:
    """Save last add failures for 'log --last'."""
    path = _failures_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(failures, f, indent=2)


def append_last_failures(
    db_path: str | Path,
    entries: list[dict[str, str]],
    *,
    max_entries: int = 50,
) -> None:
    """Append ingest failures, deduping by path (newest wins) and capping list size."""
    if not entries:
        return
    merged: dict[str, dict[str, str]] = {}
    for row in get_last_failures(db_path):
        path = str(row.get("path") or "")
        if path:
            merged[path] = {"path": path, "error": str(row.get("error") or "")}
    for row in entries:
        path = str(row.get("path") or "")
        if not path:
            continue
        merged[path] = {"path": path, "error": str(row.get("error") or "")}
    ordered = list(merged.values())
    if len(ordered) > max_entries:
        ordered = ordered[-max_entries:]
    set_last_failures(db_path, ordered)


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

_QUERY_HEALTH_MAX = 100

def record_index_error(db_path: str | Path, silo_slug: str | None, exc: Exception) -> None:
    """Append a query-time ChromaDB index error to llmli_query_health.json.

    Capped at _QUERY_HEALTH_MAX entries (oldest dropped). Safe to call from query path —
    errors writing the health file are silently swallowed so they don't mask query results.
    """
    import datetime
    path = _query_health_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        entry = {
            "silo": silo_slug or "",
            "error": f"{type(exc).__name__}: {exc}",
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "index_corruption",
        }
        existing.append(entry)
        if len(existing) > _QUERY_HEALTH_MAX:
            existing = existing[-_QUERY_HEALTH_MAX:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass  # never let health recording crash the query path

def get_query_health(db_path: str | Path) -> list[dict]:
    """Return logged query-time index errors from llmli_query_health.json."""
    path = _query_health_path(db_path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []
