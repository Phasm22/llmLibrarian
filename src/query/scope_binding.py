"""
Deterministic scope binding and lightweight catalog ranking for weak-scope retry.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

from file_registry import _read_file_manifest
from state import list_silos


SCOPE_QUERY_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "in",
        "from",
        "within",
        "for",
        "to",
        "of",
        "on",
        "is",
        "are",
        "was",
        "were",
        "what",
        "which",
        "who",
        "when",
        "where",
        "why",
        "how",
        "idea",
        "main",
    }
)


class ScopeBindingResult(TypedDict):
    bound_slug: str | None
    bound_display_name: str | None
    confidence: float
    reason: str
    cleaned_query: str


class FiletypeHints(TypedDict):
    extensions: list[str]
    reason: str | None


class SiloCandidate(TypedDict):
    slug: str
    score: float
    matched_tokens: list[str]


def _normalize_token(s: str | None) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"[-\s]+", "-", t).strip("-")
    if t.endswith("s") and len(t) > 3:
        t = t[:-1]
    return t


def _strip_hash_suffix(slug: str | None) -> str:
    s = (slug or "").strip()
    m = re.match(r"^(.+)-[0-9a-f]{8}$", s)
    return m.group(1) if m else s


def _extract_scope_candidate(query: str) -> str | None:
    q = (query or "").strip()
    if not q:
        return None
    m = re.search(r"\b(?:in|from|within)\s+(?:my\s+)?([a-zA-Z0-9][a-zA-Z0-9 _-]{0,64})", q, re.IGNORECASE)
    if not m:
        return None
    cand = m.group(1)
    # stop at common clause boundaries
    cand = re.split(r"\b(?:about|for|with|that|who|when|where|why|how|and|but|then)\b", cand, maxsplit=1, flags=re.IGNORECASE)[0]
    cand = cand.strip(" .,:;!?\"'`()[]{}")
    cand = re.sub(r"\b(folder|silo|collection)\b$", "", cand, flags=re.IGNORECASE).strip()
    return cand or None


def strip_scope_phrase(query: str) -> str:
    """Remove one scope phrase from query to reduce retrieval noise."""
    q = (query or "").strip()
    if not q:
        return ""
    out = re.sub(
        r"\b(?:in|from|within)\s+(?:my\s+)?[a-zA-Z0-9][a-zA-Z0-9 _-]{0,64}",
        "",
        q,
        count=1,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"\s{2,}", " ", out).strip(" ,")
    return out or q


def bind_scope_from_query(query: str, db_path: str) -> ScopeBindingResult:
    """Deterministically bind natural-language scope phrase to a silo slug."""
    candidate = _extract_scope_candidate(query)
    cleaned = strip_scope_phrase(query) if candidate else query
    if not candidate:
        return {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "no_scope_phrase",
            "cleaned_query": cleaned,
        }

    silos = list_silos(db_path)
    if not silos:
        return {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "no_registered_silos",
            "cleaned_query": cleaned,
        }

    cand_raw = candidate.strip().lower()
    cand_norm = _normalize_token(candidate)

    # 1.0 exact raw match against display, slug, slug-base.
    for s in silos:
        slug = str((s or {}).get("slug") or "")
        display = str((s or {}).get("display_name") or "")
        if cand_raw in {display.lower(), slug.lower(), _strip_hash_suffix(slug).lower()}:
            return {
                "bound_slug": slug,
                "bound_display_name": display or slug,
                "confidence": 1.0,
                "reason": "exact_match",
                "cleaned_query": cleaned,
            }

    # 0.9 normalized exact.
    norm_exact: list[tuple[str, str]] = []
    for s in silos:
        slug = str((s or {}).get("slug") or "")
        display = str((s or {}).get("display_name") or "")
        aliases = {_normalize_token(display), _normalize_token(slug), _normalize_token(_strip_hash_suffix(slug))}
        if cand_norm and cand_norm in aliases:
            norm_exact.append((slug, display or slug))
    if len(norm_exact) == 1:
        slug, name = norm_exact[0]
        return {
            "bound_slug": slug,
            "bound_display_name": name,
            "confidence": 0.9,
            "reason": "normalized_exact",
            "cleaned_query": cleaned,
        }
    if len(norm_exact) > 1:
        return {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "ambiguous_normalized",
            "cleaned_query": cleaned,
        }

    # 0.8 unique prefix.
    prefix_matches: list[tuple[str, str]] = []
    for s in silos:
        slug = str((s or {}).get("slug") or "")
        display = str((s or {}).get("display_name") or "")
        aliases = [_normalize_token(display), _normalize_token(slug), _normalize_token(_strip_hash_suffix(slug))]
        if any(a.startswith(cand_norm) for a in aliases if a and cand_norm):
            prefix_matches.append((slug, display or slug))
    uniq = {m[0]: m for m in prefix_matches}
    if len(uniq) == 1:
        slug, name = next(iter(uniq.values()))
        return {
            "bound_slug": slug,
            "bound_display_name": name,
            "confidence": 0.8,
            "reason": "unique_prefix",
            "cleaned_query": cleaned,
        }
    if len(uniq) > 1:
        return {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "ambiguous_prefix",
            "cleaned_query": cleaned,
        }
    return {
        "bound_slug": None,
        "bound_display_name": None,
        "confidence": 0.0,
        "reason": "no_confident_match",
        "cleaned_query": cleaned,
    }


def detect_filetype_hints(query: str) -> FiletypeHints:
    q = (query or "").lower()
    exts: list[str] = []
    reason: str | None = None
    if any(t in q for t in ("powerpoint", "pptx", "ppt", "slides", "slide deck")):
        exts.extend([".pptx", ".ppt"])
        reason = "powerpoint_terms"
    return {"extensions": exts, "reason": reason}


def _tokenize_query(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_-]{1,}", (query or "").lower())
    out: list[str] = []
    for w in words:
        if w in SCOPE_QUERY_STOPWORDS:
            continue
        if w not in out:
            out.append(w)
    return out


def rank_silos_by_catalog_tokens(query: str, db_path: str, filetype_hints: FiletypeHints) -> list[SiloCandidate]:
    """
    Rank silos using lightweight metadata only:
    - token overlap with silo display/slug
    - token overlap with sampled filenames
    - extension-hint bonus by sampled filename extensions
    """
    q_tokens = _tokenize_query(query)
    if not q_tokens:
        return []
    silos = list_silos(db_path)
    manifest = _read_file_manifest(db_path)
    manifest_silos = (manifest.get("silos") or {}) if isinstance(manifest, dict) else {}
    candidates: list[SiloCandidate] = []
    for s in silos:
        slug = str((s or {}).get("slug") or "")
        if not slug:
            continue
        display = str((s or {}).get("display_name") or "")
        name_tokens = set(_tokenize_query(f"{display} {slug} {_strip_hash_suffix(slug)}"))
        silo_entry = manifest_silos.get(slug) if isinstance(manifest_silos, dict) else None
        files_map = (silo_entry or {}).get("files") if isinstance(silo_entry, dict) else {}
        if not isinstance(files_map, dict):
            files_map = {}

        # sample top filenames (stable by path, bounded for cost)
        sample_paths = sorted(files_map.keys())[:120]
        file_tokens: set[str] = set()
        sample_exts: set[str] = set()
        for p in sample_paths:
            pp = Path(p)
            sample_exts.add(pp.suffix.lower())
            file_tokens.update(_tokenize_query(pp.name))

        overlap_name = [t for t in q_tokens if t in name_tokens]
        overlap_file = [t for t in q_tokens if t in file_tokens]
        score = (3.0 * len(overlap_name)) + (1.0 * len(overlap_file))
        if filetype_hints.get("extensions"):
            if any(ext in sample_exts for ext in filetype_hints["extensions"]):
                score += 2.0
        if score <= 0:
            continue
        candidates.append(
            {
                "slug": slug,
                "score": float(score),
                "matched_tokens": sorted(set(overlap_name + overlap_file)),
            }
        )
    candidates.sort(key=lambda c: (-c["score"], c["slug"]))
    return candidates
