"""Build Chroma where-clauses and post-filters from retrieval metadata params."""
from __future__ import annotations

from typing import Any

from frontmatter import list_values_from_meta


def normalize_tag_filters(tags: str | list[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip().lower() for t in tags.split(",") if t.strip()]
    out: list[str] = []
    for item in tags:
        if item is None:
            continue
        text = str(item).strip().lower()
        if text:
            out.append(text)
    return out


def build_metadata_where_parts(
    *,
    category: str | None = None,
    main_protein: str | None = None,
    min_servings: int | None = None,
    max_servings: int | None = None,
    max_total_time: int | None = None,
    course: str | None = None,
    era: str | None = None,
) -> list[dict[str, Any]]:
    """Equality / range clauses safe to put in a Chroma ``where`` filter."""
    parts: list[dict[str, Any]] = []
    if category:
        parts.append({"category": str(category)})
    if main_protein:
        parts.append({"main_protein": str(main_protein)})
    if course:
        parts.append({"course": str(course)})
    if era:
        parts.append({"era": str(era)})
    if min_servings is not None:
        parts.append({"servings": {"$gte": int(min_servings)}})
    if max_servings is not None:
        parts.append({"servings": {"$lte": int(max_servings)}})
    if max_total_time is not None:
        parts.append({"total_time": {"$lte": int(max_total_time)}})
    return parts


def meta_matches_tags(meta: dict[str, Any] | None, required_tags: list[str]) -> bool:
    """True when every required tag appears in the chunk's tags / dietary_flags."""
    if not required_tags:
        return True
    m = meta or {}
    haystack = set(list_values_from_meta(m.get("tags")))
    haystack.update(list_values_from_meta(m.get("dietary_flags")))
    return all(tag in haystack for tag in required_tags)


def apply_tag_postfilter(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    required_tags: list[str],
) -> tuple[list[str], list[dict | None], list[float | None]]:
    if not required_tags:
        return docs, metas, dists
    kept = [
        (d, m, dist)
        for d, m, dist in zip(docs, metas, dists)
        if meta_matches_tags(m, required_tags)
    ]
    if not kept:
        return [], [], []
    docs_o, metas_o, dists_o = zip(*kept)
    return list(docs_o), list(metas_o), list(dists_o)
