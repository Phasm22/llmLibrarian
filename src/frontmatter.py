"""Markdown YAML frontmatter → Chroma-safe scalar metadata.

Chroma metadata values must be str | int | float | bool. Lists are flattened to
comma-joined strings (with leading/trailing commas) so callers can post-filter
for membership; nested mappings are skipped. Malformed frontmatter never raises
into the ingest path — callers get ({}, original_text).
"""
from __future__ import annotations

import re
from typing import Any

# Opening --- … closing --- at the start of the file (optional BOM).
_FRONTMATTER_RE = re.compile(
    r"\A(?:\ufeff)?---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)

# Keys owned by the ingest/retrieval pipeline — never overwrite from frontmatter.
RESERVED_META_KEYS = frozenset(
    {
        "source",
        "source_path",
        "mtime",
        "chunk_hash",
        "file_id",
        "line_start",
        "is_local",
        "doc_type",
        "content_extracted",
        "section",
        "file_hash",
        "page",
        "silo",
        "zip_path",
        "source_modality",
        "parent_image_id",
        "bbox_x",
        "bbox_y",
        "bbox_w",
        "bbox_h",
        "indexed_at",
        "chunk_index",
        "record_type",
        "summary_status",
        "needs_vision_enrichment",
        "image_artifact",
        "artifact_relpath",
        "_signals",
    }
)

# Preferred order for the compact one-line preamble embedded with chunk text.
_PREAMBLE_KEYS = (
    "title",
    "category",
    "course",
    "main_protein",
    "servings",
    "total_time",
    "prep_time",
    "cook_time",
    "tags",
    "dietary_flags",
    "era",
)


def parse_markdown_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split leading YAML frontmatter from markdown body.

    Returns (raw_mapping, body). On missing/malformed frontmatter returns
    ({}, original text) so ingest continues with plain markdown chunking.
    """
    if not text:
        return {}, text
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    yaml_block = match.group(1)
    body = text[match.end() :]
    try:
        import yaml
    except ImportError:
        return {}, text
    try:
        loaded = yaml.safe_load(yaml_block)
    except Exception:
        return {}, text
    if loaded is None:
        return {}, body
    if not isinstance(loaded, dict):
        return {}, text
    return dict(loaded), body


def _scalarize(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, (dict, list, tuple, set)):
                continue
            part = str(item).strip()
            if part:
                parts.append(part)
        if not parts:
            return None
        # Delimiters on both ends make post-filter membership checks unambiguous
        # for tags that are prefixes of other tags (e.g. "veg" vs "vegan").
        return "," + ",".join(parts) + ","
    return None


def chroma_metadata_from_frontmatter(raw: dict[str, Any] | None) -> dict[str, str | int | float | bool]:
    """Flatten a frontmatter mapping into Chroma-compatible scalar metadata."""
    if not raw:
        return {}
    out: dict[str, str | int | float | bool] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        name = key.strip()
        if not name or name in RESERVED_META_KEYS or name.startswith("_"):
            continue
        scalar = _scalarize(value)
        if scalar is None:
            continue
        out[name] = scalar
    return out


def compact_frontmatter_preamble(meta: dict[str, str | int | float | bool]) -> str:
    """Short human-readable line for embedding (not the full YAML block)."""
    if not meta:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for key in _PREAMBLE_KEYS:
        if key not in meta:
            continue
        seen.add(key)
        val = meta[key]
        if isinstance(val, str) and val.startswith(",") and val.endswith(","):
            val = ", ".join(p for p in val.strip(",").split(",") if p)
        if key == "title":
            parts.append(str(val))
        elif key in ("total_time", "prep_time", "cook_time"):
            parts.append(f"{key}={val}min")
        elif key == "servings":
            parts.append(f"servings={val}")
        else:
            parts.append(f"{key}={val}")
    for key, val in meta.items():
        if key in seen:
            continue
        if isinstance(val, str) and len(val) > 80:
            continue
        if isinstance(val, str) and val.startswith(",") and val.endswith(","):
            val = ", ".join(p for p in val.strip(",").split(",") if p)
        parts.append(f"{key}={val}")
    return " | ".join(parts)


def list_values_from_meta(value: Any) -> list[str]:
    """Normalize a stored list-ish metadata value to lowercase tokens."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith(",") and text.endswith(","):
        text = text.strip(",")
    return [p.strip().lower() for p in re.split(r"[,;]", text) if p.strip()]


def frontmatter_fields_for_response(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Project non-pipeline metadata keys for MCP/retrieve responses."""
    if not meta:
        return {}
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if key in RESERVED_META_KEYS or key.startswith("_"):
            continue
        if value is None or value == "":
            continue
        if isinstance(value, str) and value.startswith(",") and value.endswith(","):
            out[key] = [p for p in value.strip(",").split(",") if p]
        else:
            out[key] = value
    return out
