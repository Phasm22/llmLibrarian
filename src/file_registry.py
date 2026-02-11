"""
File registry and manifest: track indexed files by hash, silo, and path.
Atomic JSON writes with file-level locking (fcntl on Unix).
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
    except Exception as e:
        print(f"[llmli] file manifest write failed: {path}: {e}", file=sys.stderr)
        raise


def _update_file_manifest(db_path: str | Path, update_fn: Any) -> None:
    path = _file_manifest_path(db_path)
    with _registry_lock(path):
        manifest = _read_file_manifest(db_path)
        update_fn(manifest)
        _write_file_manifest(db_path, manifest)


# --- File registry (content-hash -> [{silo, path}]) ---

def _file_registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_registry.json"
    return p.parent / "llmli_file_registry.json"


def _read_file_registry(db_path: str | Path) -> dict:
    path = _file_registry_path(db_path)
    if not path.exists():
        return {"by_hash": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data.get("by_hash"), dict) else {"by_hash": {}}
    except Exception as e:
        print(f"[llmli] file registry read failed: {path}: {e}; using empty.", file=sys.stderr)
        return {"by_hash": {}}


def _write_file_registry(db_path: str | Path, data: dict) -> None:
    path = _file_registry_path(db_path)
    try:
        _atomic_write_json(path, data)
    except Exception as e:
        print(f"[llmli] file registry write failed: {path}: {e}", file=sys.stderr)
        raise


def _update_file_registry(db_path: str | Path, update_fn: Any) -> None:
    path = _file_registry_path(db_path)
    with _registry_lock(path):
        reg = _read_file_registry(db_path)
        update_fn(reg)
        _write_file_registry(db_path, reg)


def _file_registry_get(db_path: str | Path, file_hash: str) -> list[dict]:
    """Return list of {silo, path} that have indexed this hash."""
    reg = _read_file_registry(db_path)
    return list(reg.get("by_hash", {}).get(file_hash, []))


def _file_registry_add(db_path: str | Path, file_hash: str, silo: str, path_str: str) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.setdefault("by_hash", {})
        entries = by_hash.setdefault(file_hash, [])
        if not any(e.get("silo") == silo and e.get("path") == path_str for e in entries):
            entries.append({"silo": silo, "path": path_str})

    _update_file_registry(db_path, _apply)


def _file_registry_remove_path(db_path: str | Path, silo: str, path_str: str, file_hash: str | None = None) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.get("by_hash", {})
        if file_hash and file_hash in by_hash:
            entries = by_hash.get(file_hash, [])
            entries = [e for e in entries if not (e.get("silo") == silo and e.get("path") == path_str)]
            if entries:
                by_hash[file_hash] = entries
            else:
                del by_hash[file_hash]
            return
        # Fallback: scan all hashes for the path (slower but safe).
        for h, entries in list(by_hash.items()):
            new_entries = [e for e in entries if not (e.get("silo") == silo and e.get("path") == path_str)]
            if not new_entries:
                del by_hash[h]
            else:
                by_hash[h] = new_entries

    _update_file_registry(db_path, _apply)


def _file_registry_remove_silo(db_path: str | Path, silo: str) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.get("by_hash", {})
        for h, entries in list(by_hash.items()):
            new_entries = [e for e in entries if e.get("silo") != silo]
            if not new_entries:
                del by_hash[h]
            else:
                by_hash[h] = new_entries

    _update_file_registry(db_path, _apply)


def get_paths_by_silo(db_path: str | Path) -> dict[str, set[str]]:
    """Build catalog: silo -> set of indexed paths. Derived from file registry (by_hash -> [{silo, path}])."""
    reg = _read_file_registry(db_path)
    by_silo: dict[str, set[str]] = {}
    for entries in (reg.get("by_hash") or {}).values():
        for e in entries:
            s = e.get("silo")
            p = e.get("path")
            if s is not None and p:
                by_silo.setdefault(s, set()).add(p)
    return by_silo
