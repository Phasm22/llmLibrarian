"""Shared Chroma retrieval path for MCP-style chunk lists (used by run_retrieve)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from chroma_client import get_client
from constants import LLMLI_COLLECTION, MAX_CHUNKS_PER_FILE
from embeddings import get_embedding_function
from image_embeddings import (
    get_image_embedding_adapter,
    image_collection_name,
    image_embedding_unavailable_reason,
)

from query.core_support import (
    _query_explicitly_requests_image,
    _query_image_collection,
    _query_is_image_relevant,
    _safe_query,
)
from query.intent import INTENT_EVIDENCE_PROFILE, INTENT_TAX_QUERY
from query.retrieval import (
    PROFILE_LEXICAL_PHRASES,
    dedup_by_chunk_hash,
    diversify_by_silo,
    diversify_by_source,
    merge_dual_streams_rrf,
    max_silo_chunks_for_intent,
    run_hybrid_retrieve,
    source_diversity_cap,
)
from processors import PHOTO_METADATA_FIELDS


def _artifact_stream_enabled(db: str, silo_slug: str) -> bool:
    try:
        from state import get_silo_artifact_compile

        meta = get_silo_artifact_compile(db, silo_slug) or {}
        artifact_slug = str(meta.get("artifact_silo") or "")
        return artifact_slug == f"{silo_slug}-artifacts"
    except Exception:
        return False


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

    def _where_for_silo(target_silo: str | None) -> dict | None:
        parts: list[dict[str, Any]] = []
        if target_silo:
            parts.append({"silo": target_silo})
        if doc_type:
            parts.append({"doc_type": doc_type})
        if len(parts) == 1:
            return parts[0]
        if len(parts) > 1:
            return {"$and": parts}
        return None

    def _query_stream(target_silo: str | None) -> tuple[list[str], list[dict | None], list[float | None], str | None]:
        query_kw: dict[str, Any] = {
            "query_texts": [query_for_retrieval],
            "n_results": n_stage1,
            "include": ["documents", "metadatas", "distances"],
        }
        where = _where_for_silo(target_silo)
        if where:
            query_kw["where"] = where
        docs_v, metas_v, dists_v, ids_v, _warn = _safe_query(collection, query_kw, target_silo, db_path=db_path)
        _hybrid_where = query_kw.get("where") if isinstance(query_kw.get("where"), dict) else None
        _lexical_phrases = PROFILE_LEXICAL_PHRASES if intent == INTENT_EVIDENCE_PROFILE else None
        docs_h, metas_h, dists_h, _method = run_hybrid_retrieve(
            ids_v=ids_v,
            docs_v=docs_v,
            metas_v=metas_v,
            dists_v=dists_v,
            query_text=query_for_retrieval,
            collection=collection,
            where_filter=_hybrid_where,
            top_k=n_stage1,
            lexical_phrases=_lexical_phrases,
        )
        per_cap = source_diversity_cap(
            intent,
            n_results,
            silo_scoped=bool(target_silo),
            default=MAX_CHUNKS_PER_FILE,
        )
        docs_h, metas_h, dists_h = diversify_by_source(docs_h, metas_h, dists_h, n_results, max_per_source=per_cap)
        docs_h, metas_h, dists_h = dedup_by_chunk_hash(docs_h, metas_h, dists_h)
        return docs_h, metas_h, dists_h, _warn

    docs, metas, dists, _silo_warning = _query_stream(silo_slug)
    retrieval_method = "hybrid_or_vector"
    if silo_slug and (doc_type is None or doc_type == "artifact") and _artifact_stream_enabled(db, silo_slug):
        artifact_slug = f"{silo_slug}-artifacts"
        artifact_docs, artifact_metas, artifact_dists, _artifact_warning = _query_stream(artifact_slug)
        if artifact_docs:
            docs, metas, dists = merge_dual_streams_rrf(
                docs,
                metas,
                dists,
                artifact_docs,
                artifact_metas,
                artifact_dists,
                top_k=n_results,
            )
            docs, metas, dists = dedup_by_chunk_hash(docs, metas, dists)
            retrieval_method = "dual_stream_rrf"

    image_search: dict[str, Any] = {"attempted": False, "status": "not_applicable", "matches": 0}
    explicit_image_query = _query_explicitly_requests_image(query)
    if _query_is_image_relevant(query, docs, metas):
        image_search["attempted"] = True
        image_adapter = get_image_embedding_adapter()
        if image_adapter is None:
            image_search["status"] = "unavailable"
            reason = image_embedding_unavailable_reason()
            image_search["warning"] = (
                "Image-vector search is unavailable"
                + (f": {reason}" if reason else "")
                + ". Do not treat text-only results as evidence that no matching photo exists."
            )
        else:
            image_collection = client.get_or_create_collection(name=image_collection_name(LLMLI_COLLECTION))
            image_docs, image_metas, image_dists = _query_image_collection(
                collection=collection,
                image_collection=image_collection,
                image_adapter=image_adapter,
                query_text=query_for_retrieval,
                n_results=max(2, min(8, n_results)),
                base_where=_where_for_silo(silo_slug),
                db_path=db_path or db,
            )
            image_sources = {
                str((meta or {}).get("source") or "")
                for meta in image_metas
                if str((meta or {}).get("source") or "")
            }
            image_search["matches"] = len(image_sources) or len(image_docs)
            image_search["returned_chunks"] = len(image_docs)
            image_search["status"] = "ok" if image_docs else "no_matches"
            if image_docs:
                if explicit_image_query:
                    docs, metas, dists = diversify_by_source(
                        image_docs,
                        image_metas,
                        image_dists,
                        n_results,
                        max_per_source=2,
                    )
                    retrieval_method = "image_vector"
                else:
                    docs, metas, dists = merge_dual_streams_rrf(
                        image_docs,
                        image_metas,
                        image_dists,
                        docs,
                        metas,
                        dists,
                        top_k=n_results,
                    )
                    retrieval_method = f"{retrieval_method}+image_rrf"
                docs, metas, dists = dedup_by_chunk_hash(docs, metas, dists)

    if silo_slug is None:
        per_silo_cap = n_results if explicit_image_query and image_search["status"] == "ok" else max_silo_chunks_for_intent(intent, 3)
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
        photo_metadata = {
            field: m.get(field)
            for field in PHOTO_METADATA_FIELDS
            if m.get(field) is not None
        }
        chunk = {
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
            "source_modality": m.get("source_modality"),
            "summary_status": m.get("summary_status"),
            "needs_vision_enrichment": m.get("needs_vision_enrichment"),
            "indexed_at": m.get("indexed_at"),
            "_signals": signals,
        }
        if photo_metadata:
            chunk["photo_metadata"] = photo_metadata
        chunks.append(chunk)

    result: dict = {
        "query": query,
        "intent": intent,
        "silo_filter": silo_slug,
        "retrieval_method": retrieval_method,
        "image_search": image_search,
        "chunks": chunks,
    }
    visual_follow_up = [
        str(chunk.get("source") or "")
        for chunk in chunks
        if chunk.get("source_modality") == "image"
        and chunk.get("summary_status") in {"deferred", "disabled"}
        and chunk.get("source")
    ]
    if visual_follow_up:
        result["recommended_action"] = {
            "tool": "ask_image",
            "reason": "The matched image has no visual summary; inspect the original pixels before answering visual details.",
            "files": list(dict.fromkeys(visual_follow_up))[:3],
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
