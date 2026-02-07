"""
Load archetypes.yaml and resolve archetype by id (slug). Used by indexer and query.
"""
import os
from pathlib import Path
from typing import Any

def _default_config_path() -> Path:
    base = os.environ.get("LLMLIBRARIAN_CONFIG_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / "archetypes.yaml"

def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load YAML config (archetypes + limits). Returns dict with 'archetypes' and optional 'limits'."""
    path = config_path if config_path is not None else _default_config_path()
    path = Path(path)
    if not path.exists():
        return {"archetypes": {}, "limits": {}}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data
    except Exception:
        return {"archetypes": {}, "limits": {}}

def get_archetype(config: dict[str, Any], archetype_id: str) -> dict[str, Any]:
    """Get archetype by id (slug). Raises KeyError if not found."""
    archs = config.get("archetypes") or {}
    if archetype_id not in archs:
        raise KeyError(f"Unknown archetype: {archetype_id}. Known: {list(archs.keys())}")
    arch = dict(archs[archetype_id])
    arch.setdefault("collection", f"llmli_{archetype_id}")
    arch.setdefault("name", archetype_id)
    return arch


def get_archetype_optional(config: dict[str, Any], archetype_id: str) -> dict[str, Any] | None:
    """Get archetype by id if present; else None. Use for prompt-only lookup (e.g. silo-scoped ask)."""
    archs = config.get("archetypes") or {}
    if archetype_id not in archs:
        return None
    arch = dict(archs[archetype_id])
    arch.setdefault("collection", f"llmli_{archetype_id}")
    arch.setdefault("name", archetype_id)
    return arch
