"""
Deterministic metadata aggregation queries: counts and breakdowns from manifest.
No retrieval, no LLM - purely catalog-based statistics.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from file_registry import _read_file_manifest
from style import dim, label_style


class MetadataRequest(TypedDict):
    dimension: str


class MetadataResult(TypedDict):
    aggregates: list[dict]
    dimension: str
    scanned_count: int
    scope: str
    stale: bool
    stale_reason: str | None


def parse_metadata_request(query: str) -> MetadataRequest | None:
    """
    Parse metadata aggregation queries to determine dimension.

    Dimensions:
    - year: Group by mtime year
    - month: Group by mtime month (YYYY-MM)
    - quarter: Group by mtime quarter (YYYY-Q1/Q2/Q3/Q4)
    - extension: Group by file extension (.pdf, .docx, etc.)
    - folder: Group by parent folder name

    Examples:
    - "file counts by year" → {dimension: "year"}
    - "how many documents by type" → {dimension: "extension"}
    - "extension breakdown" → {dimension: "extension"}
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    # Detect dimension
    if re.search(r'\bby\s+year\b', q):
        return {"dimension": "year"}
    elif re.search(r'\bby\s+month\b', q):
        return {"dimension": "month"}
    elif re.search(r'\bby\s+quarter\b', q):
        return {"dimension": "quarter"}
    elif re.search(r'\bby\s+(?:type|extension)\b', q) or re.search(r'\bextension\s+breakdown\b', q) or re.search(r'\bdocument\s+types?\b', q):
        return {"dimension": "extension"}
    elif re.search(r'\bby\s+folder\b', q):
        return {"dimension": "folder"}
    else:
        # Default to extension for generic counts
        return {"dimension": "extension"}


def aggregate_metadata(
    db_path: str,
    silo_slug: str,
    dimension: str,
) -> MetadataResult:
    """
    Aggregate file counts by specified dimension from manifest metadata.

    Returns list of aggregates with:
    - label: dimension value (e.g., "2024", ".pdf", "Q1-2024")
    - count: number of files
    """
    manifest = _read_file_manifest(db_path)
    silos = manifest.get("silos") or {}
    silo_entry = silos.get(silo_slug) if isinstance(silos, dict) else None

    if not isinstance(silo_entry, dict):
        return {
            "aggregates": [],
            "dimension": dimension,
            "scanned_count": 0,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": "manifest_silo_missing",
        }

    files_map = silo_entry.get("files") or {}
    if not isinstance(files_map, dict):
        return {
            "aggregates": [],
            "dimension": dimension,
            "scanned_count": 0,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": "manifest_files_missing",
        }

    # Aggregate by dimension
    counts: dict[str, int] = {}

    for path_str, meta in files_map.items():
        label: str | None = None

        if dimension == "extension":
            ext = Path(path_str).suffix.lower()
            label = ext if ext else "(no_ext)"

        elif dimension == "folder":
            parent = Path(path_str).parent.name
            label = parent if parent else "(root)"

        elif dimension in ("year", "month", "quarter"):
            mtime = (meta or {}).get("mtime")
            if mtime is not None:
                try:
                    mtime_float = float(mtime)
                    dt = datetime.fromtimestamp(mtime_float, tz=timezone.utc)

                    if dimension == "year":
                        label = str(dt.year)
                    elif dimension == "month":
                        label = dt.strftime("%Y-%m")
                    elif dimension == "quarter":
                        quarter = (dt.month - 1) // 3 + 1
                        label = f"{dt.year}-Q{quarter}"

                except (TypeError, ValueError, OSError):
                    # Skip files with invalid mtime
                    continue

        if label:
            counts[label] = counts.get(label, 0) + 1

    # Sort by count (descending), then by label (ascending)
    aggregates = [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return {
        "aggregates": aggregates,
        "dimension": dimension,
        "scanned_count": len(files_map),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


def format_metadata_answer(
    dimension: str,
    aggregates: list[dict],
    source_label: str,
    no_color: bool,
) -> str:
    """
    Format metadata aggregates as readable list.

    Output format:
    File counts by extension:
    • .pdf: 42 files
    • .docx: 28 files
    • .txt: 15 files
    ...
    """
    if not aggregates:
        return (
            f"No metadata found for {source_label}.\n\n"
            + dim(no_color, "---") + "\n"
            + label_style(no_color, f"Answered by: {source_label}")
        )

    # Format header based on dimension
    dimension_labels = {
        "year": "year",
        "month": "month",
        "quarter": "quarter",
        "extension": "extension",
        "folder": "folder",
    }
    dimension_label = dimension_labels.get(dimension, "dimension")

    lines = [f"File counts by {dimension_label}:"]
    for agg in aggregates:
        label = agg.get("label", "unknown")
        count = agg.get("count", 0)
        plural = "file" if count == 1 else "files"
        lines.append(f"• {label}: {count} {plural}")

    result = "\n".join(lines)
    result += "\n\n" + dim(no_color, "---") + "\n"
    result += label_style(no_color, f"Answered by: {source_label} (metadata)")

    return result
