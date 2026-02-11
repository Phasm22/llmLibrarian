"""
Deterministic catalog queries for file-list style asks.
Uses manifest/registry metadata only (no retrieval, no LLM).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from file_registry import _read_file_manifest
from state import list_silos


class FreshnessResult(TypedDict):
    stale: bool
    stale_reason: str | None
    scanned_count: int


class CatalogResult(TypedDict):
    files: list[str]
    scanned_count: int
    matched_count: int
    cap_applied: bool
    match_reason_counts: dict[str, int]
    scope: str
    stale: bool
    stale_reason: str | None


FILE_LIST_SKIP_WORDS = (
    "summary",
    "overview",
    "analyze",
    "analysis",
    "architecture",
    "design",
    "why",
    "how",
)


def parse_file_list_year_request(query: str) -> dict[str, str] | None:
    """Parse explicit file-list-by-year asks (e.g. 'what files are from 2022')."""
    q = (query or "").strip().lower()
    if not q:
        return None
    if any(w in q for w in FILE_LIST_SKIP_WORDS):
        return None
    if not re.search(r"\b(files?|documents?|docs?)\b", q):
        return None
    if not re.search(r"\b(list|which|what|show|find|from)\b", q):
        return None
    year_m = re.search(r"\b(20\d{2})(?!\d)\b", q)
    if not year_m:
        return None
    return {"year": year_m.group(1)}


def _path_has_year_token(path_str: str, year: int) -> bool:
    year_s = str(year)
    # Strict token boundaries: /2022/, _2022_, -2022-, ".2022."
    return bool(re.search(rf"(^|[^0-9]){re.escape(year_s)}([^0-9]|$)", path_str))


def _iter_manifest_paths_for_silo(db_path: str, silo_slug: str) -> tuple[dict[str, dict], str | None]:
    manifest = _read_file_manifest(db_path)
    silos = manifest.get("silos") or {}
    silo_entry = silos.get(silo_slug) if isinstance(silos, dict) else None
    if not isinstance(silo_entry, dict):
        return {}, "manifest_silo_missing"
    files_map = silo_entry.get("files") or {}
    if not isinstance(files_map, dict):
        return {}, "manifest_files_missing"
    return files_map, None


def validate_catalog_freshness(db_path: str, silo_slug: str) -> FreshnessResult:
    """
    Validate selected silo manifest against on-disk file metadata.
    Stale when path(s) missing or mtime/size drift from manifest.
    """
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    if err:
        return {"stale": True, "stale_reason": err, "scanned_count": 0}

    silos = list_silos(db_path)
    info = next((s for s in silos if (s or {}).get("slug") == silo_slug), None)
    if not info:
        return {"stale": True, "stale_reason": "registry_silo_missing", "scanned_count": len(files_map)}
    root = (info or {}).get("path") or ""
    if not root:
        return {"stale": True, "stale_reason": "registry_path_missing", "scanned_count": len(files_map)}
    if not Path(root).exists():
        return {"stale": True, "stale_reason": "registry_path_not_found", "scanned_count": len(files_map)}

    files_indexed = int((info or {}).get("files_indexed") or 0)
    if files_indexed != len(files_map):
        return {"stale": True, "stale_reason": "registry_manifest_count_mismatch", "scanned_count": len(files_map)}

    for path_str, meta in files_map.items():
        p = Path(path_str)
        if not p.exists():
            return {"stale": True, "stale_reason": "file_missing", "scanned_count": len(files_map)}
        try:
            st = p.stat()
        except OSError:
            return {"stale": True, "stale_reason": "file_stat_failed", "scanned_count": len(files_map)}
        prev_mtime = (meta or {}).get("mtime")
        prev_size = (meta or {}).get("size")
        if prev_mtime is None or prev_size is None:
            return {"stale": True, "stale_reason": "manifest_meta_incomplete", "scanned_count": len(files_map)}
        if st.st_mtime != prev_mtime or st.st_size != prev_size:
            return {"stale": True, "stale_reason": "file_changed_since_index", "scanned_count": len(files_map)}
    return {"stale": False, "stale_reason": None, "scanned_count": len(files_map)}


def list_files_from_year(
    db_path: str,
    silo_slug: str,
    year: int,
    *,
    year_mode: str = "mtime",
    cap: int = 50,
) -> CatalogResult:
    """
    Return deterministic catalog listing for files in a given year.
    year_mode:
      - "mtime": primary deterministic filter by mtime year
    """
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    if err:
        return {
            "files": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "match_reason_counts": {"mtime_year": 0, "path_year_token": 0},
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": err,
        }

    reason_counts = {"mtime_year": 0, "path_year_token": 0}
    matched: list[str] = []
    seen: set[str] = set()
    for path_str, meta in files_map.items():
        norm_path = str(Path(path_str).resolve())
        if norm_path in seen:
            continue
        seen.add(norm_path)

        mtime = (meta or {}).get("mtime")
        mtime_year: int | None = None
        if mtime is not None:
            try:
                mtime_year = datetime.fromtimestamp(float(mtime), tz=timezone.utc).year
            except (TypeError, ValueError, OSError):
                mtime_year = None
        path_year = _path_has_year_token(norm_path, year)
        if path_year:
            reason_counts["path_year_token"] += 1
        if mtime_year == year:
            reason_counts["mtime_year"] += 1

        if year_mode == "mtime":
            if mtime_year == year:
                matched.append(norm_path)

    matched.sort()
    capped = matched[:cap]
    return {
        "files": capped,
        "scanned_count": len(files_map),
        "matched_count": len(matched),
        "cap_applied": len(matched) > len(capped),
        "match_reason_counts": reason_counts,
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }
