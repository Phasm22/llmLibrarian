"""
Retrieval utilities: hybrid search (vector + lexical RRF), diversity, dedup,
relevance gating, and catalog sub-scope resolution.
"""
import os
import re
from pathlib import Path
from typing import Any

from constants import DEFAULT_RELEVANCE_MAX_DISTANCE

# Lexical triggers for "what do I like / what did I say / do I mention" — prefer chunks containing these.
PROFILE_TRIGGERS = re.compile(
    r"\b(I like|I love|I enjoy|I prefer|I think|I believe|my favorite|favorite|into|been into|"
    r"I (?:am |was )?(?:really )?into|I (?:would |will )?(?:choose|pick)|"
    r"said (?:that |about)|wrote (?:that |about)|mentioned|according to (?:my|me)|"
    r"in my (?:view|opinion)|I (?:feel|felt)|I (?:want|wanted)|I (?:decided|chose))\b",
    re.IGNORECASE,
)

# Hybrid search: phrases for Chroma where_document $contains (case-sensitive in Chroma). Used for EVIDENCE_PROFILE.
PROFILE_LEXICAL_PHRASES = [
    "I like", "I love", "I enjoy", "I prefer", "I think", "I believe",
    "my favorite", "favorite", "I said", "I wrote", "I feel", "I want",
    "I decided", "I chose", "in my opinion", "I mentioned",
]
RRF_K = 60  # Reciprocal Rank Fusion constant
MAX_LEXICAL_FOR_RRF = 200  # cap lexical get() results for RRF

# Per-intent source diversity caps: broader evidence profile, deeper aggregate.
DIVERSITY_CAPS: dict[str, int] = {
    "LOOKUP": 3,
    "EVIDENCE_PROFILE": 2,
    "AGGREGATE": 6,
    "REFLECT": 4,
    "FIELD_LOOKUP": 8,
}
SILO_DIVERSITY_CAPS: dict[str, int] = {
    "LOOKUP": 3,
    "EVIDENCE_PROFILE": 3,
    "AGGREGATE": 6,
    "REFLECT": 3,
}

# Catalog sub-scope: tokens we never use for path routing (avoid junk matches).
SCOPE_TOKEN_STOPLIST = frozenset(
    {"llm", "tool", "fast", "slow", "why", "the", "is", "it", "so", "a", "an", "how", "does", "feel", "take"}
)
MAX_SCOPE_TOKENS = 2  # extract up to 2 candidates; union path results
DIRECT_LEXICAL_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
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
        "in",
        "on",
        "for",
        "to",
        "of",
        "as",
        "by",
        "from",
        "latest",
        "current",
    }
)


def relevance_max_distance() -> float:
    """Max Chroma distance below which we consider chunks relevant. Env LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE; conservative default."""
    try:
        v = os.environ.get("LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE")
        if v is not None:
            return float(v)
    except (ValueError, TypeError):
        pass
    return DEFAULT_RELEVANCE_MAX_DISTANCE


def max_chunks_for_intent(intent: str, default: int) -> int:
    """Return per-intent diversity cap, falling back to the configured default."""
    cap = DIVERSITY_CAPS.get((intent or "").upper(), default)
    return cap if cap >= 1 else default


def max_silo_chunks_for_intent(intent: str, default: int) -> int:
    """Return per-intent cross-silo cap when unified retrieval spans many silos."""
    cap = SILO_DIVERSITY_CAPS.get((intent or "").upper(), default)
    return cap if cap >= 1 else default


def all_dists_above_threshold(dists: list[float | None], threshold: float) -> bool:
    """True if we have at least one distance and every non-None distance is > threshold (no chunk passed relevance bar)."""
    non_none = [d for d in dists if d is not None]
    if not non_none:
        return False
    return all(d > threshold for d in non_none)


def rrf_merge(
    ids_v: list[str],
    docs_v: list[str],
    metas_v: list[dict | None],
    dists_v: list[float | None],
    ids_l: list[str],
    docs_l: list[str],
    metas_l: list[dict | None],
    top_k: int,
    k: int = RRF_K,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Merge vector and lexical results by RRF score; return top_k (docs, metas, dists) in merged order."""
    rank_v = {vid: 1.0 / (k + (i + 1)) for i, vid in enumerate(ids_v)}
    rank_l = {lid: 1.0 / (k + (i + 1)) for i, lid in enumerate(ids_l)}
    scores: dict[str, float] = {}
    for vid in ids_v:
        scores[vid] = rank_v.get(vid, 0) + rank_l.get(vid, 0)
    for lid in ids_l:
        if lid not in scores:
            scores[lid] = rank_l.get(lid, 0)
    id_to_doc = dict(zip(ids_v, docs_v))
    id_to_meta = dict(zip(ids_v, metas_v))
    id_to_dist = dict(zip(ids_v, dists_v))
    for i, lid in enumerate(ids_l):
        if lid not in id_to_doc:
            id_to_doc[lid] = docs_l[i] if i < len(docs_l) else ""
            id_to_meta[lid] = metas_l[i] if i < len(metas_l) else None
            id_to_dist[lid] = None
    sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])[:top_k]
    return (
        [id_to_doc[i] for i in sorted_ids],
        [id_to_meta[i] for i in sorted_ids],
        [id_to_dist[i] for i in sorted_ids],
    )


def filter_by_triggers(docs: list[str], metas: list[dict | None], dists: list[float | None]) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Prefer chunks that contain profile triggers; keep order but put trigger hits first. If none match, return as-is."""
    with_triggers: list[tuple[str, dict | None, float | None]] = []
    without_triggers: list[tuple[str, dict | None, float | None]] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        dist = dists[i] if i < len(dists) else None
        if doc and PROFILE_TRIGGERS.search(doc):
            with_triggers.append((doc, meta, dist))
        else:
            without_triggers.append((doc, meta, dist))
    combined = with_triggers + without_triggers
    return (
        [c[0] for c in combined],
        [c[1] for c in combined],
        [c[2] for c in combined],
    )


def diversify_by_source(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int,
    max_per_source: int = 2,
    sources: list[str] | None = None,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Keep best chunks by distance but cap at max_per_source per unique source path so one big file doesn't dominate."""
    if not docs or max_per_source < 1:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]
    if sources is None:
        sources = [((m or {}).get("source") or "") for m in metas]
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    out_dists: list[float | None] = []
    count_per_source: dict[str, int] = {}
    for i, doc in enumerate(docs):
        if len(out_docs) >= top_k:
            break
        meta = metas[i] if i < len(metas) else None
        source = sources[i] if i < len(sources) else ((meta or {}).get("source") or "")
        if not source:
            source = f"__unknown_{i}"
        n = count_per_source.get(source, 0)
        if n >= max_per_source:
            continue
        count_per_source[source] = n + 1
        out_docs.append(doc)
        out_metas.append(meta)
        out_dists.append(dists[i] if i < len(dists) else None)
    return out_docs, out_metas, out_dists


def diversify_by_silo(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int,
    max_per_silo: int = 3,
    silos: list[str] | None = None,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Cap chunks per silo to avoid large silos dominating unified answers."""
    if not docs or max_per_silo < 1:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]
    if silos is None:
        silos = [str(((m or {}).get("silo") or "")) for m in metas]
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    out_dists: list[float | None] = []
    count_per_silo: dict[str, int] = {}
    for i, doc in enumerate(docs):
        if len(out_docs) >= top_k:
            break
        meta = metas[i] if i < len(metas) else None
        silo = silos[i] if i < len(silos) else str(((meta or {}).get("silo") or ""))
        if not silo:
            silo = f"__unknown_silo_{i}"
        n = count_per_silo.get(silo, 0)
        if n >= max_per_silo:
            continue
        count_per_silo[silo] = n + 1
        out_docs.append(doc)
        out_metas.append(meta)
        out_dists.append(dists[i] if i < len(dists) else None)
    return out_docs, out_metas, out_dists


def ensure_min_silo_coverage(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int,
    min_silos: int = 3,
    silos: list[str] | None = None,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """
    Ensure at least one chunk from up to `min_silos` distinct silos when available.

    Deterministic behavior:
    - Candidate silos are ranked by best distance, then first-seen index, then silo id.
    - Within a silo, first-seen chunk is used for coverage pass.
    - Remaining slots are filled in original order.
    """
    if not docs:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]
    if min_silos <= 1:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]
    if silos is None:
        silos = [str(((m or {}).get("silo") or "")) for m in metas]

    silo_rows: dict[str, dict[str, Any]] = {}
    ordered_rows: list[tuple[int, str, str, dict | None, float | None]] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        dist = dists[i] if i < len(dists) else None
        silo = silos[i] if i < len(silos) else str(((meta or {}).get("silo") or ""))
        if not silo:
            silo = f"__unknown_silo_{i}"
        ordered_rows.append((i, silo, doc, meta, dist))
        best_dist = float(dist) if dist is not None else 999.0
        slot = silo_rows.get(silo)
        if slot is None:
            silo_rows[silo] = {
                "first_idx": i,
                "best_dist": best_dist,
            }
            continue
        if best_dist < slot["best_dist"]:
            slot["best_dist"] = best_dist

    if not silo_rows:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]

    ranked_silos = sorted(
        silo_rows.items(),
        key=lambda kv: (kv[1]["best_dist"], kv[1]["first_idx"], kv[0]),
    )
    target_silos = {s for s, _row in ranked_silos[: min(min_silos, len(ranked_silos))]}

    selected_idx: list[int] = []
    picked_silos: set[str] = set()
    for i, silo, _doc, _meta, _dist in ordered_rows:
        if len(selected_idx) >= top_k:
            break
        if silo in target_silos and silo not in picked_silos:
            selected_idx.append(i)
            picked_silos.add(silo)

    if len(selected_idx) < top_k:
        selected_set = set(selected_idx)
        for i, _silo, _doc, _meta, _dist in ordered_rows:
            if len(selected_idx) >= top_k:
                break
            if i in selected_set:
                continue
            selected_idx.append(i)
            selected_set.add(i)

    selected_idx.sort()
    return (
        [docs[i] for i in selected_idx if i < len(docs)],
        [metas[i] if i < len(metas) else None for i in selected_idx],
        [dists[i] if i < len(dists) else None for i in selected_idx],
    )


def soft_promote_silo_diversity(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int,
    max_promotions_per_alt_silo: int = 1,
    max_total_promotions: int = 2,
    relevance_delta: float = 0.08,
    dominance_ratio: float = 0.7,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """
    Best-effort cross-silo balancing: if one silo dominates and other silos are
    near-relevant, promote a small number of alternate-silo chunks into the tail.
    """
    limit = min(top_k, len(docs))
    if limit <= 0:
        return [], [], []
    if limit < 4 or max_total_promotions < 1 or max_promotions_per_alt_silo < 1:
        return docs[:limit], (metas or [])[:limit], (dists or [])[:limit]

    silos: list[str] = []
    for i in range(len(docs)):
        silo = str(((metas[i] if i < len(metas) else None) or {}).get("silo") or "")
        silos.append(silo or f"__unknown_silo_{i}")

    count_per_silo: dict[str, int] = {}
    first_idx_per_silo: dict[str, int] = {}
    for i in range(limit):
        silo = silos[i]
        count_per_silo[silo] = count_per_silo.get(silo, 0) + 1
        if silo not in first_idx_per_silo:
            first_idx_per_silo[silo] = i

    if not count_per_silo:
        return docs[:limit], (metas or [])[:limit], (dists or [])[:limit]
    top_silo, top_count = sorted(
        count_per_silo.items(),
        key=lambda kv: (-kv[1], first_idx_per_silo.get(kv[0], 99999), kv[0]),
    )[0]
    if (top_count / float(limit)) < dominance_ratio:
        return docs[:limit], (metas or [])[:limit], (dists or [])[:limit]

    best_dist_per_silo: dict[str, float] = {}
    for i in range(len(docs)):
        silo = silos[i]
        d = float(dists[i]) if i < len(dists) and dists[i] is not None else 999.0
        if silo not in best_dist_per_silo or d < best_dist_per_silo[silo]:
            best_dist_per_silo[silo] = d

    top_best = best_dist_per_silo.get(top_silo, 999.0)
    eligible_alt_silos = {
        silo
        for silo in best_dist_per_silo.keys()
        if silo != top_silo and best_dist_per_silo.get(silo, 999.0) <= (top_best + relevance_delta)
    }
    if not eligible_alt_silos:
        return docs[:limit], (metas or [])[:limit], (dists or [])[:limit]

    base_idx = list(range(limit))
    replace_positions = [i for i in range(limit - 1, -1, -1) if silos[base_idx[i]] == top_silo]
    if not replace_positions:
        return docs[:limit], (metas or [])[:limit], (dists or [])[:limit]

    alt_candidates: list[int] = []
    for i in range(limit, len(docs)):
        if silos[i] in eligible_alt_silos:
            alt_candidates.append(i)
    alt_candidates.sort(
        key=lambda i: (
            float(dists[i]) if i < len(dists) and dists[i] is not None else 999.0,
            silos[i],
            i,
        )
    )

    promoted_per_silo: dict[str, int] = {}
    promotions = 0
    for cand in alt_candidates:
        if promotions >= max_total_promotions:
            break
        if not replace_positions:
            break
        cand_silo = silos[cand]
        used = promoted_per_silo.get(cand_silo, 0)
        if used >= max_promotions_per_alt_silo:
            continue
        pos = replace_positions.pop(0)
        base_idx[pos] = cand
        promoted_per_silo[cand_silo] = used + 1
        promotions += 1

    return (
        [docs[i] for i in base_idx if i < len(docs)],
        [metas[i] if i < len(metas) else None for i in base_idx],
        [dists[i] if i < len(dists) else None for i in base_idx],
    )


def dedup_by_chunk_hash(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Keep first occurrence of each chunk_hash; drop later duplicates. Optional (env LLMLIBRARIAN_DEDUP_CHUNK_HASH=1)."""
    if os.environ.get("LLMLIBRARIAN_DEDUP_CHUNK_HASH", "").strip().lower() not in ("1", "true", "yes"):
        return docs, metas, dists
    seen: set[str] = set()
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    out_dists: list[float | None] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        ch = (meta or {}).get("chunk_hash") or ""
        if ch and ch in seen:
            continue
        if ch:
            seen.add(ch)
        out_docs.append(doc)
        out_metas.append(meta)
        out_dists.append(dists[i] if i < len(dists) else None)
    return out_docs, out_metas, out_dists


def extract_scope_tokens(query: str) -> list[str]:
    """Extract up to 2 candidate tokens for catalog sub-scope. Stoplist applied; case-insensitive."""
    q = (query or "").strip().lower()
    if not q:
        return []
    # Patterns: "the X (llm )?tool", "why is (the )?X ...", "X (is )?fast/slow", or significant words (alpha, len >= 2).
    candidates: list[str] = []
    # "the <X> llm tool" or "the <X> tool"
    m = re.search(r"\bthe\s+(\w+)\s+(?:llm\s+)?tool\b", q)
    if m:
        candidates.append(m.group(1))
    # "why is (the )?<X> ..." (X before "fast", "slow", "so", etc.)
    m = re.search(r"\bwhy\s+is\s+(?:the\s+)?(\w+)\s+(?:so\s+)?(?:fast|slow|quick)?", q)
    if m and m.group(1) not in SCOPE_TOKEN_STOPLIST:
        if m.group(1) not in candidates:
            candidates.append(m.group(1))
    # "<X> is fast/slow" or "<X> llm tool"
    m = re.search(r"\b(\w+)\s+(?:is\s+)?(?:so\s+)?(?:fast|slow)\b", q)
    if m and m.group(1) not in SCOPE_TOKEN_STOPLIST and m.group(1) not in candidates:
        candidates.append(m.group(1))
    # Fallback: first two significant words (alpha, len >= 2) not in stoplist
    if len(candidates) < MAX_SCOPE_TOKENS:
        words = re.findall(r"\b([a-z]\w{1,})\b", q)
        for w in words:
            if w not in SCOPE_TOKEN_STOPLIST and w not in candidates:
                candidates.append(w)
                if len(candidates) >= MAX_SCOPE_TOKENS:
                    break
    return candidates[:MAX_SCOPE_TOKENS]


def resolve_subscope(
    query: str, db_path: str | Path, get_paths_by_silo: Any
) -> tuple[list[str], list[str], list[str]] | None:
    """
    Resolve query to a path sub-scope using catalog (paths-by-silo from file registry).
    Returns (silos, paths, tokens_used) when non-empty, else None.
    tokens_used is for debug (PAL_DEBUG) only.
    """
    if get_paths_by_silo is None:
        return None
    tokens = extract_scope_tokens(query)
    if not tokens:
        return None
    paths_by_silo = get_paths_by_silo(db_path)
    if not paths_by_silo:
        return None
    path_set: set[str] = set()
    silo_set: set[str] = set()
    for token in tokens:
        token_lower = token.lower()
        for silo, paths in paths_by_silo.items():
            for p in paths:
                if token_lower in p.lower():
                    path_set.add(p)
                    silo_set.add(silo)
    if not path_set:
        return None
    return (list(silo_set), list(path_set), tokens)


def extract_direct_lexical_terms(query: str, limit: int = 8) -> list[str]:
    """Extract lexical anchors for direct BM25-like fallback from user query."""
    q = (query or "").strip()
    if not q:
        return []
    quoted = re.findall(r"\"([^\"]{2,})\"", q)
    numbers = re.findall(r"\b\d+(?:[:.]\d+)?%?\b", q)
    words = re.findall(r"\b[\w-]{3,}\b", q.lower())
    terms: list[str] = []
    for tok in quoted + numbers + words:
        t = tok.strip().strip(".,:;!?")
        if not t:
            continue
        if t in DIRECT_LEXICAL_STOPWORDS:
            continue
        if t not in terms:
            terms.append(t)
        if len(terms) >= limit:
            break
    return terms


def source_priority_score(
    meta: dict | None,
    canonical_tokens: list[str],
    deprioritized_tokens: list[str],
) -> int:
    """Filename/source token priority: canonical positive, draft/archive negative."""
    source = ((meta or {}).get("source") or "").lower()
    score = 0
    if any(tok.lower() in source for tok in canonical_tokens):
        score += 10
    if any(tok.lower() in source for tok in deprioritized_tokens):
        score -= 5
    return score


def sort_by_source_priority(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    canonical_tokens: list[str],
    deprioritized_tokens: list[str],
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Stable rerank with source-priority first, distance as tie-breaker."""
    combined = list(zip(docs, metas, dists))

    def _k(item: tuple[str, dict | None, float | None]) -> tuple[int, float]:
        _doc, meta, dist = item
        return (
            -source_priority_score(meta, canonical_tokens, deprioritized_tokens),
            float(dist) if dist is not None else 999.0,
        )

    combined.sort(key=_k)
    return (
        [x[0] for x in combined],
        [x[1] for x in combined],
        [x[2] for x in combined],
    )


def source_extension_rank_map(
    metas: list[dict | None],
    dists: list[float | None],
    preferred_extensions: list[str],
) -> dict[str, int]:
    """
    Build source-level rank map for preferred extensions.
    Ranking is per unique source (not chunk):
    - preferred extension sources first
    - then by best (lowest) distance
    - then stable source path asc
    """
    exts = {e.lower() for e in preferred_extensions or [] if e}
    if not exts:
        return {}
    per_source_best: dict[str, float] = {}
    per_source_pref: dict[str, int] = {}
    for i, meta in enumerate(metas):
        source = ((meta or {}).get("source") or "").strip()
        if not source:
            continue
        dist = dists[i] if i < len(dists) and dists[i] is not None else 999.0
        if source not in per_source_best or dist < per_source_best[source]:
            per_source_best[source] = float(dist)
        suff = Path(source).suffix.lower()
        per_source_pref[source] = 1 if suff in exts else 0
    ordered = sorted(
        per_source_best.keys(),
        key=lambda s: (
            0 if per_source_pref.get(s, 0) > 0 else 1,
            per_source_best.get(s, 999.0),
            s,
        ),
    )
    return {s: idx for idx, s in enumerate(ordered)}
