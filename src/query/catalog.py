"""
Deterministic catalog queries and structure snapshots.
Uses manifest/registry metadata only (no retrieval, no LLM).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from file_registry import _read_file_manifest
from state import list_silos
from query.scope_binding import detect_filetype_hints, rank_silos_by_catalog_tokens


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


class StructureRequest(TypedDict):
    mode: str
    wants_summary: bool
    ext: str | None


class StructureResult(TypedDict):
    mode: str
    lines: list[str]
    scanned_count: int
    matched_count: int
    cap_applied: bool
    scope: str
    stale: bool
    stale_reason: str | None


class ScopeCandidate(TypedDict):
    slug: str
    display_name: str
    score: float
    matched_tokens: list[str]


class StructureExtensionCountResult(TypedDict):
    mode: str
    ext: str
    count: int
    scanned_count: int
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

STRUCTURE_SUMMARY_WORDS = (
    "summarize",
    "summary",
    "explain",
    "overview",
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


def parse_structure_request(query: str) -> StructureRequest | None:
    """Parse deterministic structure asks into submodes: outline | recent | inventory."""
    q = (query or "").strip().lower()
    if not q:
        return None
    wants_summary = any(w in q for w in STRUCTURE_SUMMARY_WORDS)
    ext_match = re.search(r"\.([a-z0-9]{1,8})\b", q)
    if (
        ext_match
        and re.search(r"\b(how\s+many|count|number\s+of)\b", q)
        and re.search(r"\b(files?|documents?|docs?)\b", q)
    ):
        return {"mode": "ext_count", "wants_summary": False, "ext": f".{ext_match.group(1)}"}
    if re.search(r"\b(recent\s+(?:changes?|files?)|what\s+changed\s+recently)\b", q):
        return {"mode": "recent", "wants_summary": wants_summary, "ext": None}
    if re.search(r"\b(file\s+types?|extensions?|inventory)\b", q):
        return {"mode": "inventory", "wants_summary": wants_summary, "ext": None}
    if re.search(r"\b(structure|folder\s+outline|outline|directory|layout|snapshot|tree)\b", q):
        return {"mode": "outline", "wants_summary": wants_summary, "ext": None}
    return None


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


def _silo_info_map(db_path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in list_silos(db_path):
        slug = str((row or {}).get("slug") or "")
        if slug:
            out[slug] = row or {}
    return out


def _relative_display_path(abs_path: str, root_path: str | None) -> str:
    p = Path(abs_path)
    if root_path:
        try:
            return str(p.resolve().relative_to(Path(root_path).resolve()))
        except Exception:
            pass
    return str(p.resolve())


def _normalize_rel_key(path_str: str) -> str:
    return str(Path(path_str).as_posix()).lower().strip()


def _parse_mtime(meta: dict | None) -> float | None:
    raw = (meta or {}).get("mtime")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _collapse_manifest_entries(files_map: dict[str, dict], root_path: str | None) -> list[dict]:
    groups: dict[str, dict] = {}
    for abs_path_raw, meta in files_map.items():
        abs_path = str(Path(abs_path_raw).resolve())
        rel_display = _relative_display_path(abs_path, root_path)
        rel_norm = _normalize_rel_key(rel_display)
        hash_v = str((meta or {}).get("hash") or "").strip().lower()
        key = f"h:{hash_v}" if hash_v else f"p:{rel_norm}"
        entry = groups.get(key)
        if entry is None:
            entry = {
                "display_path": rel_display,
                "paths": {rel_display},
                "copies": 0,
                "mtime": None,
                "ext": (Path(rel_display).suffix.lower() or "(no_ext)"),
            }
            groups[key] = entry
        entry["copies"] += 1
        entry["paths"].add(rel_display)
        # Stable label: lexicographically smallest relative path.
        entry["display_path"] = sorted(entry["paths"])[0]
        mtime = _parse_mtime(meta)
        if mtime is not None and (entry["mtime"] is None or float(mtime) > float(entry["mtime"])):
            entry["mtime"] = float(mtime)
    return list(groups.values())


def rank_scope_candidates(query: str, db_path: str, top_n: int = 3) -> list[ScopeCandidate]:
    """Deterministic top scope hints for unscoped structure asks."""
    info_map = _silo_info_map(db_path)
    requested = max(1, int(top_n))
    ranked = rank_silos_by_catalog_tokens(query, db_path, detect_filetype_hints(query))
    out: list[ScopeCandidate] = []
    chosen: set[str] = set()
    for row in ranked[:requested]:
        slug = str(row.get("slug") or "")
        if not slug:
            continue
        info = info_map.get(slug) or {}
        out.append(
            {
                "slug": slug,
                "display_name": str(info.get("display_name") or slug),
                "score": float(row.get("score") or 0.0),
                "matched_tokens": list(row.get("matched_tokens") or []),
            }
        )
        chosen.add(slug)
    # Keep UX deterministic even with low overlap: fill remaining slots by stable silo order.
    if len(out) < requested:
        for slug in sorted(info_map.keys()):
            if slug in chosen:
                continue
            info = info_map.get(slug) or {}
            out.append(
                {
                    "slug": slug,
                    "display_name": str(info.get("display_name") or slug),
                    "score": 0.0,
                    "matched_tokens": [],
                }
            )
            if len(out) >= requested:
                break
    return out


def build_structure_outline(db_path: str, silo_slug: str, cap: int = 200) -> StructureResult:
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    if err:
        return {
            "mode": "outline",
            "lines": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": err,
        }
    root = str((_silo_info_map(db_path).get(silo_slug) or {}).get("path") or "") or None
    groups = _collapse_manifest_entries(files_map, root)
    rows = []
    for g in groups:
        base = str(g.get("display_path") or "")
        copies = int(g.get("copies") or 0)
        rows.append(f"{base} ({copies} copies)" if copies > 1 else base)
    rows = sorted(set(rows))
    capped = rows[:cap]
    return {
        "mode": "outline",
        "lines": capped,
        "scanned_count": len(files_map),
        "matched_count": len(rows),
        "cap_applied": len(rows) > len(capped),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


def build_structure_recent(db_path: str, silo_slug: str, cap: int = 100) -> StructureResult:
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    if err:
        return {
            "mode": "recent",
            "lines": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": err,
        }
    root = str((_silo_info_map(db_path).get(silo_slug) or {}).get("path") or "") or None
    groups = _collapse_manifest_entries(files_map, root)
    recents: list[tuple[float, str]] = []
    for g in groups:
        mtime = g.get("mtime")
        if mtime is None:
            continue
        date_str = datetime.fromtimestamp(float(mtime), tz=timezone.utc).strftime("%Y-%m-%d")
        base = str(g.get("display_path") or "")
        copies = int(g.get("copies") or 0)
        label = f"{base} ({copies} copies)" if copies > 1 else base
        recents.append((float(mtime), f"{date_str} {label}"))
    recents.sort(key=lambda x: (-x[0], x[1]))
    rows = [x[1] for x in recents]
    capped = rows[:cap]
    return {
        "mode": "recent",
        "lines": capped,
        "scanned_count": len(files_map),
        "matched_count": len(rows),
        "cap_applied": len(rows) > len(capped),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


def build_structure_inventory(db_path: str, silo_slug: str, cap: int = 200) -> StructureResult:
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    if err:
        return {
            "mode": "inventory",
            "lines": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": err,
        }
    root = str((_silo_info_map(db_path).get(silo_slug) or {}).get("path") or "") or None
    groups = _collapse_manifest_entries(files_map, root)
    by_ext: dict[str, int] = {}
    for g in groups:
        ext = str(g.get("ext") or "(no_ext)").lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
    rows = [f"{ext} {count}" for ext, count in sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0]))]
    capped = rows[:cap]
    return {
        "mode": "inventory",
        "lines": capped,
        "scanned_count": len(files_map),
        "matched_count": len(rows),
        "cap_applied": len(rows) > len(capped),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


def build_structure_extension_count(
    db_path: str,
    silo_slug: str,
    ext: str,
) -> StructureExtensionCountResult:
    files_map, err = _iter_manifest_paths_for_silo(db_path, silo_slug)
    ext_norm = str(ext or "").strip().lower()
    if ext_norm and not ext_norm.startswith("."):
        ext_norm = f".{ext_norm}"
    if err:
        return {
            "mode": "ext_count",
            "ext": ext_norm or ".unknown",
            "count": 0,
            "scanned_count": 0,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": err,
        }
    root = str((_silo_info_map(db_path).get(silo_slug) or {}).get("path") or "") or None
    groups = _collapse_manifest_entries(files_map, root)
    count = sum(1 for g in groups if str(g.get("ext") or "").lower() == ext_norm)
    return {
        "mode": "ext_count",
        "ext": ext_norm or ".unknown",
        "count": int(count),
        "scanned_count": len(files_map),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


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
