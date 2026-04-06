"""Shared Chroma retrieval path for MCP-style chunk lists (used by run_retrieve)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from chroma_client import get_client
from constants import LLMLI_COLLECTION, MAX_CHUNKS_PER_FILE
from embeddings import get_embedding_function

from query.core_support import _safe_query
from query.intent import INTENT_EVIDENCE_PROFILE, INTENT_TAX_QUERY
from query.retrieval import (
    PROFILE_LEXICAL_PHRASES,
    dedup_by_chunk_hash,
    diversify_by_silo,
    diversify_by_source,
    max_chunks_for_intent,
    max_silo_chunks_for_intent,
    run_hybrid_retrieve,
)


def execute_retrieve_chroma_phase(
    *,
    db: str,
    intent: str,
    query: str,
    query_for_retrieval: str,
    silo_slug: str | None,
    n_stage1: int,
    n_results: int,
    section: str | None,
    doc_type: str | None,
    db_path: str | None,
    get_chroma_client: Callable[[str], Any] | None = None,
) -> dict:
    _gc = get_chroma_client or get_client
    ef = get_embedding_function(batch_size=1)
    client = _gc(str(db))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)

    query_kw: dict = {
        "query_texts": [query_for_retrieval],
        "n_results": n_stage1,
        "include": ["documents", "metadatas", "distances"],
    }
    where_parts: list[dict] = []
    if silo_slug:
        where_parts.append({"silo": silo_slug})
    if doc_type:
        where_parts.append({"doc_type": doc_type})
    if len(where_parts) == 1:
        query_kw["where"] = where_parts[0]
    elif len(where_parts) > 1:
        query_kw["where"] = {"$and": where_parts}

    docs, metas, dists, ids_v, _silo_warning = _safe_query(collection, query_kw, silo_slug, db_path=db_path)

    _hybrid_where = query_kw.get("where") if isinstance(query_kw.get("where"), dict) else None
    _lexical_phrases = PROFILE_LEXICAL_PHRASES if intent == INTENT_EVIDENCE_PROFILE else None
    docs, metas, dists, retrieval_method = run_hybrid_retrieve(
        ids_v=ids_v,
        docs_v=docs,
        metas_v=metas,
        dists_v=dists,
        query_text=query_for_retrieval,
        collection=collection,
        where_filter=_hybrid_where,
        top_k=n_stage1,
        lexical_phrases=_lexical_phrases,
    )

    per_intent_cap = max_chunks_for_intent(intent, MAX_CHUNKS_PER_FILE)
    docs, metas, dists = diversify_by_source(docs, metas, dists, n_results, max_per_source=per_intent_cap)
    docs, metas, dists = dedup_by_chunk_hash(docs, metas, dists)

    if silo_slug is None:
        per_silo_cap = max_silo_chunks_for_intent(intent, 3)
        silo_cache = [str(((m or {}).get("silo") or "")) for m in metas]
        docs, metas, dists = diversify_by_silo(
            docs, metas, dists, n_results, max_per_silo=per_silo_cap, silos=silo_cache
        )

    if section:
        section_lower = section.lower()
        filtered = [
            (d, m, dist) for d, m, dist in zip(docs, metas, dists)
            if section_lower in (m or {}).get("section", "").lower()
        ]
        if filtered:
            docs, metas, dists = zip(*filtered)

    chunks = []
    for rank, (doc, meta, dist) in enumerate(zip(docs, metas, dists), start=1):
        m = meta or {}
        signals = m.pop("_signals", None)
        mtime_raw = m.get("mtime")
        mtime_iso = None
        if mtime_raw is not None:
            try:
                mtime_iso = datetime.fromtimestamp(float(mtime_raw), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        score = None
        confidence = "low"
        if dist is not None:
            try:
                score = round(max(0.0, 1.0 - float(dist)), 4)
                confidence = "high" if score >= 0.5 else "medium" if score >= 0.2 else "low"
            except Exception:
                pass
        chunks.append({
            "rank": rank,
            "text": doc or "",
            "score": score,
            "confidence": confidence,
            "section": str(m.get("section") or ""),
            "source": str(m.get("source") or ""),
            "silo": str(m.get("silo") or ""),
            "doc_type": str(m.get("doc_type") or "other"),
            "mtime_iso": mtime_iso,
            "page": m.get("page"),
            "line_start": m.get("line_start"),
            "chunk_index": m.get("chunk_index"),
            "record_type": m.get("record_type"),
            "indexed_at": m.get("indexed_at"),
            "_signals": signals,
        })

    result: dict = {
        "query": query,
        "intent": intent,
        "silo_filter": silo_slug,
        "retrieval_method": retrieval_method,
        "chunks": chunks,
    }
    if _silo_warning:
        result["silo_warning"] = _silo_warning

    if intent == INTENT_TAX_QUERY:
        try:
            from tax.ledger import load_tax_ledger_rows
            from tax.query_contract import parse_tax_query

            parsed = parse_tax_query(query)
            requested_year: int | None = parsed.tax_year if parsed else None

            ledger_rows = load_tax_ledger_rows(
                db,
                silo=silo_slug,
                tax_year=requested_year,
            )
            if ledger_rows:
                result["tax_ledger"] = [
                    {
                        "tax_year": r.get("tax_year"),
                        "form_type": r.get("form_type"),
                        "field_label": r.get("field_label"),
                        "raw_value": r.get("raw_value"),
                        "normalized_decimal": r.get("normalized_decimal"),
                        "source": r.get("source"),
                        "page": r.get("page"),
                        "confidence": r.get("confidence"),
                        "extractor_tier": r.get("extractor_tier"),
                    }
                    for r in ledger_rows
                ]
        except Exception:
            pass

    return result
