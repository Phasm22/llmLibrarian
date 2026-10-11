"""
Retrieval utilities: hybrid search (vector + lexical RRF), diversity, dedup,
relevance gating, and catalog sub-scope resolution.
"""
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from constants import DEFAULT_RELEVANCE_MAX_DISTANCE

logger = logging.getLogger(__name__)

# Lexical triggers for "what do I like / what did I say / do I mention" — prefer chunks containing these.
PROFILE_TRIGGERS = re.compile(
    r"\b(I like|I love|I enjoy|I prefer|I think|I believe|my favorite|favorite|into|been into|"
    r"I (?:am |was )?(?:really )?into|I (?:would |will )?(?:choose|pick)|"
    r"said (?:that |about)|wrote (?:that |about)|mentioned|according to (?:my|me)|"
    r"in my (?:view|opinion)|I (?:feel|felt)|I (?:want|wanted)|I (?:decided|chose))\b",
    re.IGNORECASE,
)

# Hybrid search: lexical anchors for EVIDENCE_PROFILE. Matched case-insensitively like every lexical term.
PROFILE_LEXICAL_PHRASES = [
    "I like", "I love", "I enjoy", "I prefer", "I think", "I believe",
    "my favorite", "favorite", "I said", "I wrote", "I feel", "I want",
    "I decided", "I chose", "in my opinion", "I mentioned",
]
RRF_K = 60  # Reciprocal Rank Fusion constant
MAX_LEXICAL_FOR_RRF = 200  # cap on the BM25-sorted lexical list handed to RRF (applied after sorting)
# A term in more than this share of the scoped chunks carries no ranking signal
# ("and", "cup" in a cookbook). Below the min scope size the ratio is too coarse to trust.
LEXICAL_MAX_DF_RATIO = 0.2
LEXICAL_DF_CUTOFF_MIN_SCOPE = 50
# Documents fetched per query for in-process BM25; candidates are pre-ordered by
# IDF-weighted term coverage, so the cut only ever drops the weakest matches.
MAX_LEXICAL_SCORING_DOCS = 2000
BM25_K1 = 1.2
BM25_B = 0.75

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
# Terms are \b[\w-]{3,}\b, so only 3+ letter words need listing. This is the
# fixed floor; LEXICAL_MAX_DF_RATIO drops whatever else is common in a given scope.
DIRECT_LEXICAL_STOPWORDS = frozenset(
    """
    the and are was were what which who whom whose when where why how for from
    with without into onto about above after before over under than then there
    here this that these those they them their its his her hers our ours your
    yours you can could would should will shall may might must have has had
    having does did done doing not but nor any all some each every other also
    just only very more most much many few such been being use used using make
    makes made one get got give tell show find list please latest current
    """.split()
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


def source_diversity_cap(
    intent: str,
    n_results: int,
    *,
    silo_scoped: bool,
    default: int,
) -> int:
    """Per-source cap passed to diversify_by_source.

    LOOKUP's cap of 3 exists so one huge file cannot dominate *unified*
    retrieval. When the caller already scoped to a silo, honor n_results —
    otherwise a one-file cookbook is stuck at three hybrid hits and can drop
    the vector-#1 recipe (Classic Carrot Cake at hybrid rank 6).
    """
    per_cap = max_chunks_for_intent(intent, default)
    if silo_scoped:
        return max(per_cap, max(1, n_results))
    return per_cap


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
) -> tuple[list[str], list[dict | None], list[float | None], list[dict]]:
    """Merge vector and lexical results by RRF score; return top_k (docs, metas, dists, signals) in merged order.

    signals is a list of dicts — one per output chunk — with keys:
        vector_rank (int|None): 1-based rank in vector results, None if lexical-only
        lexical_rank (int|None): 1-based rank in lexical results, None if vector-only
        rrf_score (float): combined RRF score
    """
    pos_v = {vid: i + 1 for i, vid in enumerate(ids_v)}  # 1-based rank
    pos_l = {lid: i + 1 for i, lid in enumerate(ids_l)}
    score_v = {vid: 1.0 / (k + pos_v[vid]) for vid in ids_v}
    score_l = {lid: 1.0 / (k + pos_l[lid]) for lid in ids_l}
    scores: dict[str, float] = {}
    for vid in ids_v:
        scores[vid] = score_v[vid] + score_l.get(vid, 0.0)
    for lid in ids_l:
        if lid not in scores:
            scores[lid] = score_l[lid]
    id_to_doc = dict(zip(ids_v, docs_v))
    id_to_meta = dict(zip(ids_v, metas_v))
    id_to_dist = dict(zip(ids_v, dists_v))
    for i, lid in enumerate(ids_l):
        if lid not in id_to_doc:
            id_to_doc[lid] = docs_l[i] if i < len(docs_l) else ""
            id_to_meta[lid] = metas_l[i] if i < len(metas_l) else None
            id_to_dist[lid] = None
    sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])[:top_k]
    signals = [
        {
            "vector_rank": pos_v.get(cid),
            "lexical_rank": pos_l.get(cid),
            "rrf_score": round(scores[cid], 6),
        }
        for cid in sorted_ids
    ]
    return (
        [id_to_doc[i] for i in sorted_ids],
        [id_to_meta[i] for i in sorted_ids],
        [id_to_dist[i] for i in sorted_ids],
        signals,
    )


def merge_dual_streams_rrf(
    docs_a: list[str],
    metas_a: list[dict | None],
    dists_a: list[float | None],
    docs_b: list[str],
    metas_b: list[dict | None],
    dists_b: list[float | None],
    *,
    top_k: int,
    rrf_k: int = RRF_K,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """
    Merge two ranked result streams with reciprocal-rank scoring.

    Used for raw-vs-artifact retrieval so each stream keeps its own diversity
    policy before merge.
    """
    if not docs_a:
        return docs_b[:top_k], (metas_b or [])[:top_k], (dists_b or [])[:top_k]
    if not docs_b:
        return docs_a[:top_k], (metas_a or [])[:top_k], (dists_a or [])[:top_k]

    def _key(doc: str, meta: dict | None) -> tuple[str, str, str, str, str]:
        m = meta or {}
        return (
            doc or "",
            str(m.get("source") or ""),
            str(m.get("line_start") or ""),
            str(m.get("page") or ""),
            str(m.get("region_index") or ""),
        )

    rows: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for idx, doc in enumerate(docs_a):
        meta = metas_a[idx] if idx < len(metas_a) else None
        dist = dists_a[idx] if idx < len(dists_a) else None
        k = _key(doc, meta)
        row = rows.setdefault(k, {"doc": doc, "meta": meta, "dist": dist, "score": 0.0})
        row["score"] += 1.0 / (rrf_k + idx + 1)
        if row.get("dist") is None and dist is not None:
            row["dist"] = dist
    for idx, doc in enumerate(docs_b):
        meta = metas_b[idx] if idx < len(metas_b) else None
        dist = dists_b[idx] if idx < len(dists_b) else None
        k = _key(doc, meta)
        row = rows.setdefault(k, {"doc": doc, "meta": meta, "dist": dist, "score": 0.0})
        row["score"] += 1.0 / (rrf_k + idx + 1)
        if row.get("dist") is None and dist is not None:
            row["dist"] = dist

    ranked = sorted(
        rows.values(),
        key=lambda item: (
            -float(item.get("score") or 0.0),
            float(item.get("dist")) if item.get("dist") is not None else 999.0,
        ),
    )[:top_k]
    return (
        [str(r.get("doc") or "") for r in ranked],
        [r.get("meta") if isinstance(r.get("meta"), dict) or r.get("meta") is None else None for r in ranked],
        [r.get("dist") if isinstance(r.get("dist"), (int, float)) or r.get("dist") is None else None for r in ranked],
    )


def lexical_term_pattern(term: str) -> str:
    """Case-insensitive regex for one lexical term, valid in both Chroma ($regex) and Python re.

    Anchored at a leading word boundary so "corn" does not match "acorn", but not at
    the end, so "chicken" still matches "chickens".
    """
    escaped = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", term)
    lead = "\\b" if re.match(r"\w", term) else ""
    return f"(?i){lead}{escaped}"


def _lexical_scope_size(collection: Any, where_filter: dict | None) -> int:
    if not where_filter:
        return int(collection.count())
    return len(collection.get(where=where_filter, include=[]).get("ids") or [])


def rank_lexical_candidates(
    collection: Any,
    terms: list[str],
    where_filter: dict | None,
    limit: int = MAX_LEXICAL_FOR_RRF,
) -> tuple[list[str], list[str], list[dict | None], dict[str, Any]]:
    """Return lexical hits ordered by BM25, best first, plus diagnostics.

    RRF reads list position as relevance, so the order here is the whole contract.
    A filtered ``collection.get`` returns storage order; ranking that directly let
    the first-indexed file supply every lexical rank 1 (docs/RCA_HYBRID_LEXICAL_RANKING_RECIPES_SILO.md).

    1. Per term, fetch matching ids only (case-insensitive ``$regex``) to get document frequency.
    2. Drop terms that match nothing, or that match more than LEXICAL_MAX_DF_RATIO of the scope.
    3. Pre-order the union by IDF-weighted term coverage; fetch the top MAX_LEXICAL_SCORING_DOCS.
    4. Score those with BM25 (term frequency, length normalization), sort, then cap at ``limit``.
    """
    diag: dict[str, Any] = {"terms_used": [], "terms_dropped": {}}
    base_kw: dict[str, Any] = {"where": where_filter} if where_filter else {}
    scope_size = _lexical_scope_size(collection, where_filter)
    diag["scope_size"] = scope_size

    df_ids: dict[str, set[str]] = {}
    patterns: dict[str, str] = {}
    for term in terms:
        pattern = lexical_term_pattern(term)
        hit = collection.get(where_document={"$regex": pattern}, include=[], **base_kw)
        ids = set(hit.get("ids") or [])
        if not ids:
            diag["terms_dropped"][term] = "no_match"
            continue
        if scope_size >= LEXICAL_DF_CUTOFF_MIN_SCOPE and len(ids) > LEXICAL_MAX_DF_RATIO * scope_size:
            diag["terms_dropped"][term] = f"too_common ({len(ids)}/{scope_size} chunks)"
            continue
        df_ids[term] = ids
        patterns[term] = pattern
    diag["terms_used"] = list(df_ids)
    if not df_ids:
        diag["status"] = "no_discriminative_terms"
        return [], [], [], diag

    n = max(scope_size, max(len(ids) for ids in df_ids.values()))
    idf = {t: math.log(1.0 + (n - len(ids) + 0.5) / (len(ids) + 0.5)) for t, ids in df_ids.items()}
    coverage: dict[str, float] = {}
    for term, ids in df_ids.items():
        for cid in ids:
            coverage[cid] = coverage.get(cid, 0.0) + idf[term]
    # Sort by id within equal coverage so the cut is deterministic, never storage order.
    shortlist = sorted(coverage, key=lambda cid: (-coverage[cid], cid))[:MAX_LEXICAL_SCORING_DOCS]
    diag["candidates"] = len(coverage)
    diag["scored"] = len(shortlist)

    fetched = collection.get(ids=shortlist, include=["documents", "metadatas"])
    f_ids: list[str] = list(fetched.get("ids") or [])
    f_docs: list[str] = list(fetched.get("documents") or [])
    f_metas: list[dict | None] = list(fetched.get("metadatas") or [])
    compiled = {t: re.compile(p) for t, p in patterns.items()}
    lengths = [max(1, len((d or "").split())) for d in f_docs]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0

    scored: list[tuple[float, float, str, int]] = []
    for i, (cid, doc) in enumerate(zip(f_ids, f_docs)):
        text = doc or ""
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * lengths[i] / avgdl)
        score = 0.0
        for term, rx in compiled.items():
            if cid not in df_ids[term]:
                continue
            tf = len(rx.findall(text)) or 1  # Chroma matched it; Python's regex disagreeing is no reason to drop it
            score += idf[term] * tf * (BM25_K1 + 1.0) / (tf + norm)
        scored.append((score, coverage.get(cid, 0.0), cid, i))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    top = scored[:limit]
    diag["status"] = "ranked"
    diag["hits"] = len(top)
    return (
        [f_ids[i] for *_, i in top],
        [f_docs[i] if i < len(f_docs) else "" for *_, i in top],
        [f_metas[i] if i < len(f_metas) else None for *_, i in top],
        diag,
    )


def run_hybrid_retrieve(
    ids_v: list[str],
    docs_v: list[str],
    metas_v: list[dict | None],
    dists_v: list[float | None],
    query_text: str,
    collection: Any,
    where_filter: dict | None,
    top_k: int,
    lexical_phrases: list[str] | None = None,
    diagnostics: dict | None = None,
) -> tuple[list[str], list[dict | None], list[float | None], str]:
    """Run hybrid retrieval: combine pre-executed vector results with a BM25-ranked lexical list via RRF.

    Falls back to vector-only when ids_v is empty, no terms are extracted, every term is
    too common or unmatched, or the lexical lookup raises (logged, never silent).

    lexical_phrases — if provided, use these as lexical anchors (e.g. PROFILE_LEXICAL_PHRASES
    for EVIDENCE_PROFILE intent). If None, terms are extracted from query_text automatically.

    diagnostics — if a dict is passed, it is filled with what the lexical leg did:
    terms_extracted, terms_used, terms_dropped, status, and counts.

    Injects ``_signals`` into each returned meta dict with keys:
        vector_rank, lexical_rank, rrf_score  (all None for vector-only path)

    Returns (docs, metas, dists, retrieval_method) where retrieval_method is ``"hybrid"`` or ``"vector_only"``.
    """
    if lexical_phrases is not None:
        terms = list(lexical_phrases)
    else:
        terms = extract_direct_lexical_terms(query_text)

    lexical: dict[str, Any] = {"terms_extracted": list(terms)}
    if not terms:
        lexical["status"] = "no_terms"
    elif not ids_v:
        lexical["status"] = "no_vector_results"
    else:
        try:
            ids_l, docs_l, metas_l, lex_diag = rank_lexical_candidates(collection, terms, where_filter)
        except Exception as exc:
            logger.warning(
                "lexical retrieval failed, falling back to vector-only: %s: %s", type(exc).__name__, exc
            )
            lexical["status"] = "error"
            lexical["error"] = f"{type(exc).__name__}: {exc}"
        else:
            lexical.update(lex_diag)
            if ids_l:
                docs, metas, dists, signals = rrf_merge(
                    ids_v, docs_v, metas_v, dists_v,
                    ids_l, docs_l, metas_l,
                    top_k=top_k,
                )
                for meta, sig in zip(metas, signals):
                    if meta is not None:
                        meta["_signals"] = sig
                if diagnostics is not None:
                    diagnostics.update(lexical)
                return docs, metas, dists, "hybrid"
    if diagnostics is not None:
        diagnostics.update(lexical)

    # Vector-only fallback: annotate with positional signals
    for i, meta in enumerate(metas_v):
        if meta is not None:
            meta["_signals"] = {"vector_rank": i + 1, "lexical_rank": None, "rrf_score": None}
    return docs_v, metas_v, dists_v, "vector_only"


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
    """Keep first occurrence of each chunk_hash; drop later duplicates. On by default; disable with LLMLIBRARIAN_DEDUP_CHUNK_HASH=0."""
    if os.environ.get("LLMLIBRARIAN_DEDUP_CHUNK_HASH", "1").strip().lower() in ("0", "false", "no"):
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
    """Extract lexical anchors (quoted phrases, numbers, words) for the BM25 lexical leg.

    Lowercased and deduplicated: matching is case-insensitive, so case carries nothing.
    """
    q = (query or "").strip()
    if not q:
        return []
    quoted = re.findall(r"\"([^\"]{2,})\"", q)
    numbers = re.findall(r"\b\d+(?:[:.]\d+)?%?\b", q)
    words = re.findall(r"\b[\w-]{3,}\b", q.lower())
    terms: list[str] = []
    for tok in quoted + numbers + words:
        t = tok.strip().strip(".,:;!?").lower()
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


def sort_by_image_chunk_priority(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """
    Keep source ordering stable, but within one image source prefer:
    image_summary first, then the best matching image_region chunks.
    """
    if not docs:
        return docs, metas, dists
    source_first_index: dict[str, int] = {}
    for i, meta in enumerate(metas):
        source = str(((meta or {}).get("source") or f"__unknown_{i}"))
        if source not in source_first_index:
            source_first_index[source] = i

    combined = list(zip(docs, metas, dists, range(len(docs))))

    def _k(item: tuple[str, dict | None, float | None, int]) -> tuple[int, int, float, int, int]:
        _doc, meta, dist, idx = item
        m = meta or {}
        source = str(m.get("source") or f"__unknown_{idx}")
        source_rank = source_first_index.get(source, idx)
        if str(m.get("source_modality") or "") != "image":
            return (source_rank, 0, float(dist) if dist is not None else 999.0, 0, idx)
        record_type = str(m.get("record_type") or "").lower()
        type_rank = 0 if record_type == "image_summary" else 1 if record_type == "image_region" else 2
        region_index = int(m.get("region_index") or 0)
        return (source_rank, type_rank, float(dist) if dist is not None else 999.0, region_index, idx)

    combined.sort(key=_k)
    return (
        [x[0] for x in combined],
        [x[1] for x in combined],
        [x[2] for x in combined],
    )


_ACADEMIC_DEPRIORITIZED_TOKENS = (
    "transfer",
    "best choices",
    "agreement",
    "articulation",
    "four-year plan",
    "four year plan",
    "degree plan",
    "suggested",
)
_ACADEMIC_TRANSCRIPT_SOURCE_DENYLIST = ("syllabus", "agreement", "transfer", "best choices", "four-year plan", "four year plan")


def _person_tokens(name: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z]{2,}", (name or "").lower())}


def _names_overlap(lhs: str, rhs: str) -> bool:
    left = _person_tokens(lhs)
    right = _person_tokens(rhs)
    if not left or not right:
        return False
    overlap = left & right
    if len(overlap) >= 2:
        return True
    return bool(overlap and (len(left) == 1 or len(right) == 1))


def _normalize_school_key(value: str) -> str:
    raw = (value or "").lower()
    key = re.sub(r"[^a-z0-9]", "", raw)
    if not key:
        return ""
    if (
        "uccs" in key
        or "cucolosprings" in key
        or "cucoloradosprings" in key
        or "universityofcoloradocoloradosprings" in key
    ):
        return "uccs"
    return key


def academic_source_priority_score(
    meta: dict | None,
    doc: str,
    query_contract: dict[str, Any] | None,
) -> int:
    """
    Bounded academic source-priority score.
    Promotes transcript/audit records and softly deprioritizes plan/guide sources.
    """
    m = meta or {}
    source = str(m.get("source") or "").lower()
    record_type = str(m.get("record_type") or "").lower()
    doc_type = str(m.get("doc_type") or "").lower()
    text = (doc or "").lower()
    contract = query_contract or {}
    user_name = str(contract.get("user_name") or "").strip()
    requested_school = str(contract.get("requested_school") or "").strip()

    score = 0
    if record_type == "transcript_row":
        score += 12
    elif record_type == "audit_row":
        score += 8
    elif record_type == "plan_row":
        score += 2

    if doc_type == "transcript":
        score += 4
    elif doc_type == "audit":
        score += 2

    if "course row:" in text:
        score += 2

    if record_type == "transcript_row" and any(tok in source for tok in _ACADEMIC_TRANSCRIPT_SOURCE_DENYLIST):
        # Suspicious transcript_row from clearly non-transcript docs.
        score -= 18

    if any(tok in source for tok in _ACADEMIC_DEPRIORITIZED_TOKENS):
        score -= 4
    if "plan_row" == record_type:
        score -= 2

    if user_name:
        student_name = str(m.get("student_name") or "")
        if record_type == "transcript_row":
            if student_name and _names_overlap(student_name, user_name):
                score += 6
            else:
                score -= 6
        elif student_name and _names_overlap(student_name, user_name):
            score += 2

    if requested_school:
        requested_key = _normalize_school_key(requested_school)
        school_key = _normalize_school_key(str(m.get("course_school") or ""))
        if not school_key:
            school_key = _normalize_school_key(source)
        if requested_key and school_key:
            if school_key == requested_key:
                score += 4
            else:
                score -= 3

    return max(-12, min(24, score))


def sort_by_academic_priority(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    query_contract: dict[str, Any] | None,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Stable rerank for class-history fallback queries. Never excludes sources."""
    combined = list(zip(docs, metas, dists))

    def _k(item: tuple[str, dict | None, float | None]) -> tuple[int, float, str, str, str]:
        doc, meta, dist = item
        m = meta or {}
        return (
            -academic_source_priority_score(meta, doc, query_contract),
            float(dist) if dist is not None else 999.0,
            str(m.get("source") or ""),
            str(m.get("page") or ""),
            str(m.get("line_start") or ""),
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
