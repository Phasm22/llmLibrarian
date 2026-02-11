"""
Load archetypes.yaml and resolve archetype by id (slug). Used by indexer and query.
"""
import os
import sys
from pathlib import Path
from typing import TypedDict


class ArchetypeConfig(TypedDict, total=False):
    name: str
    collection: str
    folders: list[str]
    include: list[str]
    exclude: list[str]


class LimitsConfig(TypedDict, total=False):
    max_file_size_mb: int | float
    max_depth: int
    max_archive_size_mb: int | float
    max_files_per_zip: int
    max_extracted_bytes_per_zip: int


class AppConfig(TypedDict):
    archetypes: dict[str, ArchetypeConfig]
    limits: LimitsConfig
    query: dict[str, object]

def _default_config_path() -> Path:
    base = os.environ.get("LLMLIBRARIAN_CONFIG_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / "archetypes.yaml"

def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load YAML config (archetypes + limits). Returns dict with 'archetypes' and optional 'limits'. Warns on missing file or load failure."""
    path = config_path if config_path is not None else _default_config_path()
    path = Path(path)
    if not path.exists():
        print(f"[llmli] config not found: {path}; using empty archetypes and limits.", file=sys.stderr)
        return {"archetypes": {}, "limits": {}}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data
    except Exception as e:
        print(f"[llmli] config load failed: {path}: {e}; using empty archetypes and limits.", file=sys.stderr)
        return {"archetypes": {}, "limits": {}}

def get_archetype(config: AppConfig, archetype_id: str) -> ArchetypeConfig:
    """Get archetype by id (slug). Raises KeyError if not found."""
    archs = config.get("archetypes") or {}
    if archetype_id not in archs:
        raise KeyError(f"Unknown archetype: {archetype_id}. Known: {list(archs.keys())}")
    arch = dict(archs[archetype_id])
    arch.setdefault("collection", f"llmli_{archetype_id}")
    arch.setdefault("name", archetype_id)
    return arch


def get_archetype_optional(config: AppConfig, archetype_id: str) -> ArchetypeConfig | None:
    """Get archetype by id if present; else None. Use for prompt-only lookup (e.g. silo-scoped ask)."""
    archs = config.get("archetypes") or {}
    if archetype_id not in archs:
        return None
    arch = dict(archs[archetype_id])
    arch.setdefault("collection", f"llmli_{archetype_id}")
    arch.setdefault("name", archetype_id)
    return arch


def get_query_options(config: AppConfig | dict | None) -> dict[str, object]:
    """Return query behavior options with stable defaults."""
    q = ((config or {}).get("query") or {}) if isinstance(config, dict) else {}
    return {
        "auto_scope_binding": bool(q.get("auto_scope_binding", True)),
        "direct_decisive_mode": bool(q.get("direct_decisive_mode", False)),
        "canonical_filename_tokens": list(q.get("canonical_filename_tokens") or ["canonical", "official"]),
        "deprioritized_tokens": list(q.get("deprioritized_tokens") or ["draft", "archive", "stale", "deprecated"]),
        "confidence_relaxation_enabled": bool(q.get("confidence_relaxation_enabled", True)),
    }
