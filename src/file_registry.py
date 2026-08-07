"""
File manifest state and derived file registry indexes.

``llmli_file_manifest.json`` is the source of truth for indexed files. The
legacy hash registry shape (``{"by_hash": ...}``) is derived from the manifest
for callers that need fast content-hash lookup.
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    fcntl = None  # type: ignore[assignment]

_derived_registry_cache: dict[str, tuple[int, int, dict]] = {}


# --- Low-level helpers ---

def _registry_lock_path(registry_path: Path) -> Path:
    if registry_path.suffix:
        return registry_path.with_suffix(registry_path.suffix + ".lock")
    return registry_path.with_name(registry_path.name + ".lock")


@contextmanager
def _registry_lock(registry_path: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path = _registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# --- File manifest (per-silo file mtime/size tracking) ---

def _file_manifest_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_manifest.json"
    return p.parent / "llmli_file_manifest.json"


def _read_file_manifest(db_path: str | Path) -> dict:
    path = _file_manifest_path(db_path)
    if not path.exists():
        return {"silos": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "silos" not in data:
                return {"silos": {}}
            return data
    except Exception as e:
        print(f"[llmli] file manifest read failed: {path}: {e}; using empty.", file=sys.stderr)
        return {"silos": {}}


def _write_file_manifest(db_path: str | Path, data: dict) -> None:
    path = _file_manifest_path(db_path)
    try:
        _atomic_write_json(path, data)
        _derived_registry_cache.pop(str(path), None)
    except Exception as e:
        print(f"[llmli] file manifest write failed: {path}: {e}", file=sys.stderr)
        raise
    _retire_legacy_registry(db_path)


def _update_file_manifest(db_path: str | Path, update_fn: Any) -> None:
    path = _file_manifest_path(db_path)
    with _registry_lock(path):
        manifest = _read_file_manifest(db_path)
        update_fn(manifest)
        _write_file_manifest(db_path, manifest)


def manifest_file_entry(
    path_str: str,
    mtime: float,
    size: int,
    file_hash: str,
) -> dict[str, Any]:
    """Build a manifest entry with parsed filename-date fields.

    Imported lazily to avoid a circular import (query.filename_dates -> formatting
    -> nothing in file_registry; this is just defensive).
    """
    from query.filename_dates import parse_filename_date

    name_date, precision = parse_filename_date(path_str)
    entry: dict[str, Any] = {"mtime": mtime, "size": size, "hash": file_hash}
    if name_date:
        entry["name_date"] = name_date
        entry["name_date_precision"] = precision
    return entry


# --- Derived file registry (content-hash -> [{silo, path}]) ---

def _legacy_registry_path(db_path: str | Path) -> Path:
    """Where the retired ``llmli_file_registry.json`` used to live.

    Only ``_retire_legacy_registry`` uses this. The file is no longer read or
    written; the manifest holds every field it contained.
    """
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_registry.json"
    return p.parent / "llmli_file_registry.json"


def _retire_legacy_registry(db_path: str | Path) -> None:
    """Delete a leftover ``llmli_file_registry.json`` if one is still present.

    Nothing reads it, so leaving it behind is worse than removing it: a stale
    copy of what the manifest already says invites a future reader to trust it.

    Failures are swallowed on purpose — an unwritable DB directory is not a
    reason to fail an ingest over a file nothing reads. A long-running process
    that loaded the pre-derive module keeps recreating this until it restarts.
    """
    try:
        _legacy_registry_path(db_path).unlink(missing_ok=True)
    except OSError:
        pass


def _registry_from_manifest(manifest: dict) -> dict:
    by_hash: dict[str, list[dict[str, str]]] = {}
    silos = manifest.get("silos") or {}
    if not isinstance(silos, dict):
        return {"by_hash": by_hash}
    for silo, silo_entry in silos.items():
        if not isinstance(silo_entry, dict):
            continue
        files = silo_entry.get("files") or {}
        if not isinstance(files, dict):
            continue
        for path_str, meta in files.items():
            if not isinstance(meta, dict):
                continue
            file_hash = str(meta.get("hash") or "")
            if not file_hash:
                continue
            by_hash.setdefault(file_hash, []).append({"silo": str(silo), "path": str(path_str)})
    return {"by_hash": by_hash}


def _manifest_cache_key(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _read_file_registry(db_path: str | Path) -> dict:
    """Return the legacy registry shape, derived from the file manifest."""
    manifest_path = _file_manifest_path(db_path)
    cache_key = _manifest_cache_key(manifest_path)
    path_key = str(manifest_path)
    cached = _derived_registry_cache.get(path_key)
    if cached and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
        return cached[2]
    reg = _registry_from_manifest(_read_file_manifest(db_path))
    _derived_registry_cache[path_key] = (cache_key[0], cache_key[1], reg)
    return reg


def _file_registry_get(db_path: str | Path, file_hash: str) -> list[dict]:
    """Return list of {silo, path} that have indexed this hash.

    This is the one lookup the derived index exists for: "is this content
    already indexed anywhere?", which the manifest cannot answer without a full
    scan. Everything else reads the manifest directly.
    """
    reg = _read_file_registry(db_path)
    return list(reg.get("by_hash", {}).get(file_hash, []))


def get_paths_by_silo(db_path: str | Path) -> dict[str, set[str]]:
    """Build catalog: silo -> set of indexed paths from the manifest."""
    manifest = _read_file_manifest(db_path)
    silos = manifest.get("silos") or {}
    by_silo: dict[str, set[str]] = {}
    if not isinstance(silos, dict):
        return by_silo
    for silo, silo_entry in silos.items():
        if not isinstance(silo_entry, dict):
            continue
        files = silo_entry.get("files") or {}
        if not isinstance(files, dict):
            continue
        by_silo.setdefault(str(silo), set()).update(str(path_str) for path_str in files.keys())
    return by_silo
