"""
Helpers for query.core: Chroma-safe query, image hydration, confidence scoring,
and prompt utilities. Extracted from core.py for maintainability.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from processors import _build_image_summary_text

from query.context import query_mentioned_years, context_block
from query.intent import INTENT_FIELD_LOOKUP, INTENT_LOOKUP, INTENT_TAX_QUERY
from query.retrieval import sort_by_image_chunk_priority


def _qc() -> Any:
    import query.core as qc

    return qc


def _ingest_read_artifact(db_path: str, relpath: str) -> dict[str, Any] | None:
    fn = getattr(_qc(), "_read_image_artifact", None)
    if not callable(fn):
        return None
    return fn(db_path, relpath)


def _ingest_update_artifact(db_path: str, relpath: str, payload: dict[str, Any]) -> None:
    fn = getattr(_qc(), "_update_image_artifact", None)
    if callable(fn):
        fn(db_path, relpath, payload)


def _vision_summarize(image_bytes: bytes, source_path: str, visible_text: str) -> tuple[str, str | None]:
    fn = getattr(_qc(), "_summarize_image_with_vision_model", None)
    if not callable(fn):
        return "", None
    return fn(image_bytes, source_path, visible_text)


def _get_silo_image_vision_enabled(db_path: str, slug: str) -> bool:
    fn = getattr(_qc(), "get_silo_image_vision_enabled", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(db_path, slug))
    except Exception:
        return False


class QueryPolicyError(Exception):
    """User-facing deterministic query policy error with exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


WEAK_SCOPE_TOP_DISTANCE = 0.70


def _top_distance(dists: list[float | None]) -> float | None:
    for d in dists:
        if d is not None:
            return float(d)
    return None


def _single_year_from_query(query: str) -> int | None:
    years = query_mentioned_years(query)
    if len(years) != 1:
        return None
    try:
        return int(years[0])
    except (TypeError, ValueError):
        return None


def _is_code_activity_year_lookup(query: str) -> bool:
    """
    Detect "what was I coding/programming in YYYY" style asks.
    These should use year+code constrained retrieval, not language-only summaries.
    """
    q = (query or "").strip().lower()
    if not q:
        return False
    if _single_year_from_query(q) is None:
        return False
    if re.search(r"\b(files?|documents?|docs?)\b", q):
        return False
    return bool(
        re.search(
            r"\bwhat\s+was\s+i\s+(?:coding|programming)\b|"
            r"\bwhat\s+did\s+i\s+(?:code|program)\b",
            q,
        )
    )


_UNIFIED_ANALYTICAL_PATTERN = re.compile(
    r"\b("
    r"across\s+silos|"
    r"recurring\s+themes?|"
    r"repeatedly|"
    r"versus|"
    r"\bvs\b|"
    r"abandoned|"
    r"likely\s+next\s+step|"
    r"llm[- ]oriented\s+workflows?|"
    r"traditional\s+workflows?"
    r")\b",
    re.IGNORECASE,
)

_TIMELINE_RECENCY_PATTERN = re.compile(
    r"\b("
    r"timeline|"
    r"shift|"
    r"changed?|"
    r"over\s+time|"
    r"abandon(?:ed|ment)?|"
    r"stale|"
    r"inactive|"
    r"last\s+touched|"
    r"stopped|"
    r"pending"
    r")\b",
    re.IGNORECASE,
)

_OWNERSHIP_PATTERN = re.compile(
    r"\b("
    r"mine|"
    r"my\s+authored|"
    r"authored|"
    r"i\s+wrote|"
    r"my\s+work|"
    r"not\s+mine|"
    r"reference|"
    r"course|"
    r"vendor|"
    r"ownership"
    r")\b",
    re.IGNORECASE,
)
_IMAGE_QUERY_PATTERN = re.compile(
    r"\b("
    r"image|images|photo|photos|picture|pictures|"
    r"screenshot|screenshots|screen|ui|visual|logo|icon|"
    r"dog|cat|person|face|scene|object|objects|"
    r"shown|showing|looks\s+like|appears\s+in"
    r")\b",
    re.IGNORECASE,
)


def _is_unified_analytical_query(query: str, intent: str) -> bool:
    from query.intent import (
        INTENT_AGGREGATE,
        INTENT_EVIDENCE_PROFILE,
        INTENT_LOOKUP,
        INTENT_REFLECT,
    )

    if intent not in (INTENT_LOOKUP, INTENT_AGGREGATE, INTENT_REFLECT, INTENT_EVIDENCE_PROFILE):
        return False
    return bool(_UNIFIED_ANALYTICAL_PATTERN.search(query or ""))


def _query_requests_recency_hints(query: str) -> bool:
    return bool(_TIMELINE_RECENCY_PATTERN.search(query or ""))


def _query_requests_ownership_framing(query: str) -> bool:
    return bool(_OWNERSHIP_PATTERN.search(query or ""))


def _combine_where_and(base_where: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
    if not base_where:
        return extra
    if "$and" in base_where and isinstance(base_where.get("$and"), list):
        return {"$and": [*list(base_where["$and"]), extra]}
    return {"$and": [base_where, extra]}


def _is_chroma_index_error(exc: Exception) -> bool:
    """Detect ChromaDB HNSW index/metadata mismatch errors (e.g. 'Error finding id')."""
    msg = str(exc).lower()
    return "finding id" in msg or "internalerror" in type(exc).__name__.lower()


def _safe_query(
    collection: Any,
    query_kw: dict[str, Any],
    silo_slug: str | None = None,
    db_path: str | None = None,
) -> tuple[list[str], list[dict | None], list[float | None], list[str], str | None]:
    """
    Run collection.query(**query_kw) with a graceful fallback when ChromaDB throws an
    index-consistency error (e.g. 'InternalError: Error finding id').

    When the scoped query fails and a silo_slug is set, retries without the where filter
    and post-filters results to the target silo in Python.  Returns (docs, metas, dists,
    ids, warning) where warning is None on success or a human-readable string on fallback.
    ChromaDB always returns ids from .query() regardless of the include list.
    """
    try:
        results = collection.query(**query_kw)
        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []
        ids = (results.get("ids") or [[]])[0] or []
        return docs, metas, dists, ids, None
    except Exception as exc:
        if not _is_chroma_index_error(exc):
            raise
        # Silo index is inconsistent — fall back to unscoped query + Python post-filter.
        warning = (
            f"Silo '{silo_slug}' has a ChromaDB index inconsistency ({type(exc).__name__}: {exc}). "
            "Results were retrieved globally and filtered to this silo in Python. "
            "Fix: run `llmli repair <silo>` or `llmli add --full <folder>` to re-index."
        )
        print(f"[llmli WARNING] {warning}", file=sys.stderr)
        if db_path:
            try:
                from state import record_index_error as _record

                _record(db_path, silo_slug, exc)
            except Exception:
                pass
        fallback_kw = {k: v for k, v in query_kw.items() if k != "where"}
        results = collection.query(**fallback_kw)
        docs = (results.get("documents") or [[]])[0] or []
        metas = (results.get("metadatas") or [[]])[0] or []
        dists = (results.get("distances") or [[]])[0] or []
        ids = (results.get("ids") or [[]])[0] or []
        if silo_slug:
            filtered = [
                (d, m, dist, cid)
                for d, m, dist, cid in zip(docs, metas, dists, ids or [""] * len(docs))
                if str((m or {}).get("silo") or "") == silo_slug
            ]
            if filtered:
                docs, metas, dists, ids = zip(*filtered)  # type: ignore[assignment]
                docs, metas, dists, ids = list(docs), list(metas), list(dists), list(ids)
            else:
                docs, metas, dists, ids = [], [], [], []
        return docs, metas, dists, ids, warning


def _query_is_image_relevant(query: str, docs: list[str], metas: list[dict | None]) -> bool:
    if _IMAGE_QUERY_PATTERN.search(query or ""):
        return True
    return any(str((meta or {}).get("source_modality") or "") == "image" for meta in metas[:4]) or not docs


def _meta_allows_image_vision(db_path: str, meta: dict[str, Any] | None) -> bool:
    slug = str((meta or {}).get("silo") or "").strip()
    if not slug:
        return False
    return _get_silo_image_vision_enabled(db_path, slug)


def _hydrate_single_image_summary_doc(
    *,
    doc: str,
    meta: dict[str, Any] | None,
    db_path: str,
    allow_lazy: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    meta_dict = dict(meta or {})
    if str(meta_dict.get("record_type") or "") != "image_summary":
        return doc, meta
    relpath = str(meta_dict.get("image_artifact_relpath") or "")
    if not relpath:
        return doc, meta
    artifact = _ingest_read_artifact(db_path, relpath)
    if not artifact:
        return doc, meta

    if not _meta_allows_image_vision(db_path, meta_dict):
        meta_dict["summary_status"] = "disabled"
        meta_dict["needs_vision_enrichment"] = False
        return doc, meta_dict

    summary_status = str(artifact.get("summary_status") or meta_dict.get("summary_status") or "").strip().lower()
    visible_text = str(artifact.get("visible_text") or "")
    summary_text = str(artifact.get("summary") or "").strip()
    if summary_status in {"eager", "cached_query_time"} and summary_text:
        meta_dict["summary_status"] = summary_status
        meta_dict["needs_vision_enrichment"] = False
        if artifact.get("vision_model"):
            meta_dict["vision_model"] = artifact.get("vision_model")
        return _build_image_summary_text(summary_text, visible_text), meta_dict

    if summary_status != "deferred" or not allow_lazy:
        return doc, meta

    source_path = str(meta_dict.get("source") or artifact.get("source_path") or "").strip()
    if not source_path:
        return doc, meta
    try:
        image_bytes = Path(source_path).read_bytes()
    except OSError:
        return doc, meta
    try:
        summary_text, vision_model = _vision_summarize(image_bytes, source_path, visible_text)
    except Exception:
        return doc, meta

    artifact["summary"] = summary_text
    artifact["summary_status"] = "cached_query_time"
    artifact["vision_model"] = vision_model or artifact.get("vision_model")
    artifact["query_time_summary_cached_at"] = datetime.now(timezone.utc).isoformat()
    _ingest_update_artifact(db_path, relpath, artifact)

    meta_dict["summary_status"] = "cached_query_time"
    meta_dict["needs_vision_enrichment"] = False
    if vision_model:
        meta_dict["vision_model"] = vision_model
    return _build_image_summary_text(summary_text, visible_text), meta_dict


def _hydrate_image_summary_docs(
    *,
    docs: list[str],
    metas: list[dict | None],
    db_path: str,
    max_lazy: int = 1,
) -> tuple[list[str], list[dict | None], int]:
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    lazy_used = 0
    for idx, doc in enumerate(docs):
        hydrated_doc, hydrated_meta = _hydrate_single_image_summary_doc(
            doc=doc,
            meta=metas[idx] if idx < len(metas) else None,
            db_path=db_path,
            allow_lazy=lazy_used < max_lazy,
        )
        if (
            hydrated_meta
            and str((hydrated_meta or {}).get("record_type") or "") == "image_summary"
            and str((hydrated_meta or {}).get("summary_status") or "") == "cached_query_time"
            and str(((metas[idx] if idx < len(metas) else None) or {}).get("summary_status") or "") == "deferred"
        ):
            lazy_used += 1
        out_docs.append(hydrated_doc)
        out_metas.append(hydrated_meta)
    return out_docs, out_metas, lazy_used


def _query_image_collection(
    *,
    collection: Any,
    image_collection: Any,
    image_adapter: Any,
    query_text: str,
    n_results: int,
    base_where: dict[str, Any] | None,
    db_path: str,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    query_kw: dict[str, Any] = {
        "query_embeddings": image_adapter.embed_texts([query_text]),
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"],
    }
    if base_where:
        query_kw["where"] = base_where
    try:
        image_results = image_collection.query(**query_kw)
    except Exception:
        return [], [], []
    image_metas = (image_results.get("metadatas") or [[]])[0] or []
    image_dists = (image_results.get("distances") or [[]])[0] or []
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    out_dists: list[float | None] = []
    seen_parents: set[str] = set()
    lazy_budget = 1
    for idx, meta in enumerate(image_metas):
        meta = meta if isinstance(meta, dict) else {}
        parent_image_id = str(meta.get("parent_image_id") or "")
        if not parent_image_id or parent_image_id in seen_parents:
            continue
        seen_parents.add(parent_image_id)
        where = _combine_where_and(base_where, {"parent_image_id": parent_image_id}) if base_where else {"parent_image_id": parent_image_id}
        try:
            text_results = collection.query(
                query_texts=[query_text],
                n_results=4,
                include=["documents", "metadatas", "distances"],
                where=where,
            )
        except Exception:
            continue
        docs = (text_results.get("documents") or [[]])[0] or []
        metas = (text_results.get("metadatas") or [[]])[0] or []
        dists = (text_results.get("distances") or [[]])[0] or []
        if not docs:
            continue
        docs, metas, hydrated = _hydrate_image_summary_docs(
            docs=list(docs),
            metas=list(metas),
            db_path=db_path,
            max_lazy=lazy_budget,
        )
        lazy_budget = max(0, lazy_budget - hydrated)
        docs, metas, dists = sort_by_image_chunk_priority(docs, metas, dists)
        for j, doc in enumerate(docs[:2]):
            out_docs.append(doc)
            out_metas.append(metas[j] if j < len(metas) else None)
            image_dist = image_dists[idx] if idx < len(image_dists) else None
            text_dist = dists[j] if j < len(dists) else None
            if image_dist is not None and text_dist is not None:
                out_dists.append((float(image_dist) + float(text_dist)) / 2.0)
            else:
                out_dists.append(image_dist if image_dist is not None else text_dist)
    return out_docs, out_metas, out_dists


def _rank_candidate_silos(
    metas: list[dict | None],
    dists: list[float | None],
    max_candidates: int = 6,
) -> list[str]:
    by_silo: dict[str, tuple[float, int]] = {}
    for i, meta in enumerate(metas):
        silo = str((meta or {}).get("silo") or "").strip()
        if not silo:
            continue
        dist = float(dists[i]) if i < len(dists) and dists[i] is not None else 999.0
        prev = by_silo.get(silo)
        if prev is None or dist < prev[0]:
            by_silo[silo] = (dist, i)
    ranked = sorted(by_silo.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    return [s for s, _ in ranked[:max_candidates]]


def _distinct_silo_count(metas: list[dict | None]) -> int:
    return len(
        {
            str((m or {}).get("silo") or "").strip()
            for m in metas
            if str((m or {}).get("silo") or "").strip()
        }
    )


def _format_mtime_utc(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _build_recency_hints(metas: list[dict | None], cap: int = 6) -> list[str]:
    buckets: dict[str, dict[str, Any]] = {}
    for meta in metas:
        silo = str((meta or {}).get("silo") or "").strip() or "unknown"
        raw_mtime = (meta or {}).get("mtime")
        if raw_mtime is None:
            continue
        try:
            mtime = float(raw_mtime)
        except (TypeError, ValueError):
            continue
        slot = buckets.setdefault(silo, {"newest": mtime, "oldest": mtime, "count": 0})
        slot["newest"] = max(float(slot["newest"]), mtime)
        slot["oldest"] = min(float(slot["oldest"]), mtime)
        slot["count"] = int(slot["count"]) + 1

    ranked = sorted(buckets.items(), key=lambda kv: (-float(kv[1]["newest"]), kv[0]))[:cap]
    lines: list[str] = []
    for silo, row in ranked:
        newest = _format_mtime_utc(float(row["newest"]))
        oldest = _format_mtime_utc(float(row["oldest"]))
        if not newest:
            continue
        if oldest and oldest != newest:
            lines.append(f"- {silo}: newest {newest}, oldest {oldest}, mtime samples={int(row['count'])}")
        else:
            lines.append(f"- {silo}: newest {newest}, mtime samples={int(row['count'])}")
    return lines


def _group_context_by_silo(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    show_silo_in_context: bool,
) -> str:
    if not docs:
        return ""
    groups: dict[str, list[int]] = {}
    for i, _doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        silo = str((meta or {}).get("silo") or "").strip() or "unknown"
        groups.setdefault(silo, []).append(i)

    def _best_dist(idxs: list[int]) -> float:
        vals = [float(dists[i]) for i in idxs if i < len(dists) and dists[i] is not None]
        if not vals:
            return 999.0
        return min(vals)

    ordered_silos = sorted(groups.keys(), key=lambda s: (_best_dist(groups[s]), groups[s][0], s))
    sections: list[str] = []
    for silo in ordered_silos:
        parts = [f"[SILO GROUP: {silo}]"]
        for i in groups[silo]:
            doc = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) else None
            if not doc:
                continue
            parts.append(context_block(doc, meta, show_silo_in_context))
        sections.append("\n---\n".join(parts))
    return "\n\n====\n\n".join(sections)


def _distinct_source_count(metas: list[dict | None]) -> int:
    return len({((m or {}).get("source") or "") for m in metas if ((m or {}).get("source") or "")})


_OVERLAP_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "what",
        "which",
        "when",
        "where",
        "your",
        "about",
        "mentioned",
        "files",
        "file",
        "docs",
        "documents",
        "list",
        "technical",
    }
)


def _has_query_evidence_overlap(query: str, docs: list[str], metas: list[dict | None]) -> bool:
    """
    True when query tokens overlap with retrieved evidence text or source paths.
    Helps avoid false-negative structure fallback on scoped queries where semantic
    distance is high but lexical anchors (e.g., filenames like "resume") match.
    """
    q_tokens = {
        t
        for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (query or "").lower())
        if t not in _OVERLAP_STOPWORDS and not t.isdigit()
    }
    if not q_tokens:
        return False

    evidence_tokens: set[str] = set()
    for doc in docs[:16]:
        evidence_tokens.update(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (doc or "").lower()))
    for meta in metas[:16]:
        src = str((meta or {}).get("source") or "")
        if src:
            evidence_tokens.update(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", src.lower()))
    return bool(q_tokens.intersection(evidence_tokens))


_UNCERTAINTY_ANSWER_PATTERN = re.compile(
    r"\b("
    r"there is no mention|"
    r"not in the provided context|"
    r"couldn[\u0027\u2019]t find|"
    r"could not find|"
    r"i don[\u0027\u2019]t have enough evidence|"
    r"i do not have enough evidence"
    r")\b",
    re.IGNORECASE,
)


def _query_overlap_support(query: str, docs: list[str], metas: list[dict | None], cap: int = 8) -> float:
    """
    Return max query-token overlap ratio against top evidence chunks.
    Value in [0, 1]; lower values indicate weaker lexical support concentration.
    """
    q_tokens = {
        t
        for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (query or "").lower())
        if t not in _OVERLAP_STOPWORDS and not t.isdigit()
    }
    if not q_tokens:
        return 0.0

    best = 0.0
    for i in range(min(cap, len(docs))):
        doc = docs[i] if i < len(docs) else ""
        meta = metas[i] if i < len(metas) else None
        src = str((meta or {}).get("source") or "")
        evidence_tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", ((doc or "") + " " + src).lower()))
        if not evidence_tokens:
            continue
        overlap_ratio = len(q_tokens.intersection(evidence_tokens)) / max(1, len(q_tokens))
        if overlap_ratio > best:
            best = overlap_ratio
    return best


def _academic_support_stats(metas: list[dict | None]) -> tuple[int, int]:
    """Return (transcript_or_audit_hits, academic_row_count) from retrieved metas."""
    support_hits = 0
    row_count = 0
    for meta in metas:
        m = meta or {}
        record_type = str(m.get("record_type") or "").lower()
        doc_type = str(m.get("doc_type") or "").lower()
        if record_type in {"transcript_row", "audit_row", "plan_row"}:
            row_count += 1
        if record_type in {"transcript_row", "audit_row"} or doc_type in {"transcript", "audit"}:
            support_hits += 1
    return (support_hits, row_count)


def _confidence_assessment(
    dists: list[float | None],
    metas: list[dict | None],
    intent: str,
    query: str,
    docs: list[str] | None = None,
    explicit_unified: bool = False,
    direct_canonical_available: bool = False,
    confidence_relaxation_enabled: bool = True,
    filetype_hints: list[str] | None = None,
    answer_text: str | None = None,
    academic_mode: bool = False,
    academic_transcript_hits: int = 0,
) -> dict[str, Any]:
    """Return confidence warning + diagnostics used for trace observability."""
    non_none = [float(d) for d in dists if d is not None]
    if not non_none:
        return {
            "warning": None,
            "reason": "no_distances",
            "top_distance": None,
            "avg_distance": None,
            "source_count": _distinct_source_count(metas),
            "overlap_support": _query_overlap_support(query, docs or [], metas),
        }

    avg_distance = sum(non_none) / len(non_none)
    top_distance = min(non_none) if non_none else None
    unique_sources = _distinct_source_count(metas)
    overlap_support = _query_overlap_support(query, docs or [], metas)
    query_overlap = _has_query_evidence_overlap(query, docs or [], metas)
    structured_support = any(
        bool((m or {}).get("field_code"))
        or bool((m or {}).get("extractor_tier"))
        or bool((m or {}).get("record_type"))
        or bool((m or {}).get("ocr_mode"))
        for m in metas
    )
    if not structured_support:
        ql = (query or "").lower()
        if re.search(r"\b(w-?2|1099|1040|tax|box\s+\d+|line\s+\d+)\b", ql):
            structured_support = any(
                re.search(r"\b(form\s+1040|w-?2|1099|box\s+\d+|line\s+\d+)\b", str(doc or ""), re.IGNORECASE)
                for doc in (docs or [])[:4]
            )

    warning: str | None = None
    reason: str | None = None
    if avg_distance > 0.7:
        if intent == INTENT_LOOKUP and _is_code_activity_year_lookup(query):
            warning = None
            reason = "relaxed_code_activity_year"
        elif intent == INTENT_LOOKUP and direct_canonical_available and confidence_relaxation_enabled:
            warning = None
            reason = "relaxed_canonical"
        elif (
            intent == INTENT_LOOKUP
            and query_overlap
            and not explicit_unified
            and top_distance is not None
            and top_distance <= 0.85
            and overlap_support >= 0.18
        ):
            warning = None
            reason = "relaxed_overlap"
        elif intent == INTENT_TAX_QUERY and structured_support:
            warning = None
            reason = "relaxed_tax_query_structured"
        elif structured_support and (query_overlap or direct_canonical_available):
            warning = None
            reason = "relaxed_structured"
        else:
            ql = (query or "").lower()
            is_broad_synthesis = bool(
                re.search(r"\b(timeline|across|compare|synthesi[sz]e|major events)\b", ql)
            )
            if explicit_unified and is_broad_synthesis:
                warning = (
                    "Low confidence: unified search found weak or uneven evidence across silos. "
                    "Try narrowing by silo/time/type or splitting into sub-questions."
                )
                reason = "weak_unified_broad"
            else:
                hint_exts = {str(e).lower() for e in (filetype_hints or [])}
                if ".pptx" in hint_exts or ".ppt" in hint_exts:
                    pres_sources = {
                        ((m or {}).get("source") or "")
                        for m in metas
                        if (((m or {}).get("source") or "").lower().endswith(".pptx") or ((m or {}).get("source") or "").lower().endswith(".ppt"))
                    }
                    if len(pres_sources) > 1:
                        warning = "Low confidence: matched multiple presentations; answer is based on the closest match."
                        reason = "weak_multi_presentation"
                if warning is None:
                    warning = "Low confidence: query is weakly related to indexed content."
                    reason = "weak_distance"
    elif intent == INTENT_FIELD_LOOKUP:
        warning = None
        reason = "field_lookup_suppressed"
    elif unique_sources == 1:
        if direct_canonical_available or query_overlap or structured_support:
            warning = None
            reason = "single_source_relaxed"
        else:
            warning = "Single source: answer is based on one document only."
            reason = "single_source"

    if academic_mode and warning and academic_transcript_hits <= 0:
        warning = "Low confidence: class-history query is not grounded in transcript/audit course rows in this scope."
        reason = "academic_no_transcript_support"

    # If the model's answer is uncertainty/absence language and evidence quality is mixed,
    # force the standard low-confidence banner for user-facing consistency.
    if answer_text and _UNCERTAINTY_ANSWER_PATTERN.search(answer_text):
        mixed_noisy = (
            unique_sources >= 3
            and (top_distance is not None and top_distance >= 0.35)
            and avg_distance >= 0.45
            and overlap_support <= 0.50
        )
        if mixed_noisy:
            warning = "Low confidence: query is weakly related to indexed content."
            reason = "uncertainty_mixed_retrieval"

    return {
        "warning": warning,
        "reason": reason,
        "top_distance": top_distance,
        "avg_distance": avg_distance,
        "source_count": unique_sources,
        "overlap_support": overlap_support,
    }


def _confidence_signal(
    dists: list[float | None],
    metas: list[dict | None],
    intent: str,
    query: str,
    docs: list[str] | None = None,
    explicit_unified: bool = False,
    direct_canonical_available: bool = False,
    confidence_relaxation_enabled: bool = True,
    filetype_hints: list[str] | None = None,
    answer_text: str | None = None,
    academic_mode: bool = False,
    academic_transcript_hits: int = 0,
) -> str | None:
    """Emit a lightweight confidence warning when retrieval quality is weak."""
    assessment = _confidence_assessment(
        dists=dists,
        metas=metas,
        intent=intent,
        query=query,
        docs=docs,
        explicit_unified=explicit_unified,
        direct_canonical_available=direct_canonical_available,
        confidence_relaxation_enabled=confidence_relaxation_enabled,
        filetype_hints=filetype_hints,
        answer_text=answer_text,
        academic_mode=academic_mode,
        academic_transcript_hits=academic_transcript_hits,
    )
    return assessment.get("warning")


def _quiet_text_only(text: str) -> str:
    """Strip footer sections for quiet mode output."""
    if not text:
        return ""
    marker = "\n\n---"
    if marker in text:
        return text.split(marker, 1)[0].strip()
    return text.strip()


def _normalize_silo_token(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s


def _strip_hash_suffix(slug: str | None) -> str | None:
    if not slug:
        return None
    m = re.match(r"^(.+)-[0-9a-f]{8}$", slug)
    if not m:
        return slug
    return m.group(1)


def _resolve_unified_silo_prompt(
    db: str,
    config_path: str | Path | None,
    silo: str,
) -> tuple[str, str]:
    qc = _qc()
    default_prompt = "Answer only from the provided context. Be concise."
    display_name = qc.get_silo_display_name(db, silo)

    override = qc.get_silo_prompt_override(db, silo)
    if override and override.strip():
        return override, (display_name or silo)

    candidates: list[str] = []
    for c in (
        silo,
        _strip_hash_suffix(silo),
        _normalize_silo_token(display_name),
    ):
        if c and c not in candidates:
            candidates.append(c)

    arch = None
    try:
        config = qc.load_config(config_path)
        for candidate in candidates:
            arch = qc.get_archetype_optional(config, candidate)
            if arch is not None:
                break
    except Exception:
        arch = None

    if arch and arch.get("prompt"):
        return (arch.get("prompt") or default_prompt), (arch.get("name") or display_name or silo)
    return default_prompt, (display_name or silo)


def _llm_summarize_structure(
    model: str,
    query: str,
    source_label: str,
    mode_label: str,
    lines: list[str],
) -> str | None:
    if not lines:
        return None
    try:
        import ollama

        preview = "\n".join(lines[:120])
        resp = ollama.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize only the provided deterministic structure snapshot. "
                        "Do not invent files or dates. Keep it concise."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Query: {query}\n"
                        f"Scope: {source_label}\n"
                        f"Snapshot type: {mode_label}\n\n"
                        "[START SNAPSHOT]\n"
                        f"{preview}\n"
                        "[END SNAPSHOT]"
                    ),
                },
            ],
            keep_alive=0,
            options={"temperature": 0, "seed": 42},
        )
        text = ((resp.get("message") or {}).get("content") or "").strip()
        return text or None
    except Exception:
        return None


def _compose_answer_system_prompt(base_prompt: str, voice_policy: str) -> str:
    parts = [voice_policy.strip()]
    base = (base_prompt or "").strip()
    if base:
        parts.append(base)
    parts.append("If the context does not contain the answer, state that clearly but remain helpful.")
    return "\n\n".join(parts)


def _repair_direct_address_answer(
    model: str,
    query: str,
    answer: str,
    violations: list[str],
) -> str:
    if not answer or not violations:
        return answer
    repair_system_prompt = (
        "You minimally rewrite assistant answers to satisfy a direct-address contract. "
        "Allowed edits only: replace third-person user stand-ins with 'you' or 'your', "
        "fix the verb immediately attached to 'you' when needed for grammar, and remove copied control markers. "
        "Do not paraphrase, summarize, add context, or remove content. "
        "Keep every other word, date, number, relationship term, quote, bullet, heading, and source reference unchanged. "
        "Examples: \"the narrator's girlfriend\" -> \"your girlfriend\". "
        "\"The narrator acknowledges this.\" -> \"You acknowledge this.\" "
        "\"The person reflecting in the journal entries values this relationship.\" -> \"You value this relationship.\" "
        "\"The person often hangs out with him.\" -> \"You often hang out with him.\" "
        "Return only the repaired answer text."
    )
    repair_user_prompt = (
        f"Question: {query}\n"
        f"Violations to fix: {', '.join(violations)}\n"
        "Rewrite the original answer with only the allowed edits.\n\n"
        f"Original answer:\n{answer}"
    )
    try:
        import ollama

        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": repair_system_prompt},
                {"role": "user", "content": repair_user_prompt},
            ],
            keep_alive=0,
            options={"temperature": 0, "seed": 42},
        )
        repaired = ((response.get("message") or {}).get("content") or "").strip()
        repaired = repaired.replace("[START ANSWER]", "").replace("[END ANSWER]", "").strip()
        repaired = re.sub(r"^\s*(?:repaired answer|original answer)\s*:\s*", "", repaired, flags=re.IGNORECASE)
        return repaired or answer
    except Exception:
        return answer
