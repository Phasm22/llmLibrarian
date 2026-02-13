"""
Deterministic timeline queries: chronological sequences from manifest metadata.
No retrieval, no LLM - purely catalog-based temporal ordering.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from file_registry import _read_file_manifest
from style import bold, dim, label_style


class TimelineRequest(TypedDict):
    start_year: int | None
    end_year: int | None
    keywords: list[str]


class TimelineResult(TypedDict):
    events: list[dict]
    scanned_count: int
    matched_count: int
    cap_applied: bool
    scope: str
    stale: bool
    stale_reason: str | None


def parse_timeline_request(query: str) -> TimelineRequest | None:
    """
    Parse timeline queries to extract year range and keywords.

    Examples:
    - "timeline of project milestones 2023-2024" → {start_year: 2023, end_year: 2024, keywords: ["project", "milestones"]}
    - "chronological history of design docs" → {start_year: None, end_year: None, keywords: ["design", "docs"]}
    - "sequence of events in 2024" → {start_year: 2024, end_year: 2024, keywords: ["events"]}
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    # Extract year range
    start_year: int | None = None
    end_year: int | None = None

    # Pattern 1: Year range (2023-2024 or 2023 to 2024)
    year_range_match = re.search(r'\b(20\d{2})\s*[-–to]\s*(20\d{2})\b', q)
    if year_range_match:
        start_year = int(year_range_match.group(1))
        end_year = int(year_range_match.group(2))
    else:
        # Pattern 2: Single year
        single_year_match = re.search(r'\b(20\d{2})\b', q)
        if single_year_match:
            start_year = int(single_year_match.group(1))
            end_year = start_year

    # Extract keywords (filter out timeline trigger words and common words)
    stopwords = {
        "timeline", "chronolog", "chronological", "sequence", "history", "evolution",
        "progression", "of", "the", "in", "from", "to", "and", "or", "a", "an",
        "what", "when", "how", "why", "is", "are", "was", "were", "events", "milestones",
        "changes", "updates", str(start_year) if start_year else "", str(end_year) if end_year else ""
    }
    tokens = re.findall(r'\b\w+\b', q)
    keywords = [t for t in tokens if t not in stopwords and len(t) > 2]

    return {
        "start_year": start_year,
        "end_year": end_year,
        "keywords": keywords[:5],  # Limit to 5 keywords for filtering
    }


def build_timeline_from_manifest(
    db_path: str,
    silo_slug: str,
    start_year: int | None,
    end_year: int | None,
    keywords: list[str],
    cap: int = 100,
) -> TimelineResult:
    """
    Build chronological timeline from file manifest metadata.

    Returns list of events with:
    - date: YYYY-MM-DD
    - path: file path (relative display)
    - silo: silo slug
    """
    manifest = _read_file_manifest(db_path)
    silos = manifest.get("silos") or {}
    silo_entry = silos.get(silo_slug) if isinstance(silos, dict) else None

    if not isinstance(silo_entry, dict):
        return {
            "events": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": "manifest_silo_missing",
        }

    files_map = silo_entry.get("files") or {}
    if not isinstance(files_map, dict):
        return {
            "events": [],
            "scanned_count": 0,
            "matched_count": 0,
            "cap_applied": False,
            "scope": f"silo:{silo_slug}",
            "stale": True,
            "stale_reason": "manifest_files_missing",
        }

    # Build timeline from manifest
    events: list[dict] = []
    for path_str, meta in files_map.items():
        mtime = (meta or {}).get("mtime")
        if mtime is None:
            continue

        try:
            mtime_float = float(mtime)
            dt = datetime.fromtimestamp(mtime_float, tz=timezone.utc)
            year = dt.year

            # Filter by year range if specified
            if start_year is not None and year < start_year:
                continue
            if end_year is not None and year > end_year:
                continue

            # Filter by keywords if specified
            path_lower = path_str.lower()
            if keywords:
                has_keyword = any(kw in path_lower for kw in keywords)
                if not has_keyword:
                    continue

            # Format event
            date_str = dt.strftime("%Y-%m-%d")
            display_path = str(Path(path_str).name)  # Just the filename for brevity

            events.append({
                "date": date_str,
                "timestamp": mtime_float,
                "path": display_path,
                "full_path": path_str,
                "silo": silo_slug,
            })

        except (TypeError, ValueError, OSError):
            # Skip files with invalid mtime
            continue

    # Sort chronologically by timestamp
    events.sort(key=lambda e: e["timestamp"])

    # Apply cap
    matched_count = len(events)
    capped_events = events[:cap]

    return {
        "events": capped_events,
        "scanned_count": len(files_map),
        "matched_count": matched_count,
        "cap_applied": matched_count > len(capped_events),
        "scope": f"silo:{silo_slug}",
        "stale": False,
        "stale_reason": None,
    }


def format_timeline_answer(events: list[dict], source_label: str, no_color: bool) -> str:
    """
    Format timeline events as chronological list.

    Output format:
    • YYYY-MM-DD: filename.ext
    • YYYY-MM-DD: another_file.pdf
    ...
    """
    if not events:
        return (
            f"No events found in timeline for {source_label}.\n\n"
            + dim(no_color, "---") + "\n"
            + label_style(no_color, f"Answered by: {source_label}")
        )

    lines = []
    for event in events:
        date_str = event.get("date", "unknown")
        path = event.get("path", "unknown")
        lines.append(f"• {date_str}: {path}")

    result = "\n".join(lines)
    result += "\n\n" + dim(no_color, "---") + "\n"
    result += label_style(no_color, f"Answered by: {source_label} (timeline)")

    return result
