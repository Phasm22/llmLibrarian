"""
Silo registry and last-failures for llmli add/ls/log. Stored next to DB path.
"""
import json
from pathlib import Path
from typing import Any

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
    silo_name: str,
    folder_path: str,
    files_indexed: int,
    chunks_count: int,
    updated_iso: str,
) -> None:
    """Record silo after add. Uses slug (silo_name) as key."""
    reg = _read_registry(db_path)
    reg[silo_name] = {
        "slug": silo_name,
        "path": folder_path,
        "files_indexed": files_indexed,
        "chunks_count": chunks_count,
        "updated": updated_iso,
    }
    _write_registry(db_path, reg)

def list_silos(db_path: str | Path) -> list[dict[str, Any]]:
    """Return list of silo dicts (slug, path, files_indexed, chunks_count, updated)."""
    reg = _read_registry(db_path)
    return list(reg.values())

def remove_silo(db_path: str | Path, silo_name: str) -> bool:
    """Remove silo from registry. Returns True if removed. Does not delete from Chroma; caller does that."""
    reg = _read_registry(db_path)
    if silo_name not in reg:
        return False
    del reg[silo_name]
    _write_registry(db_path, reg)
    return True

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
