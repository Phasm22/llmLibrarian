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
from state import private_filter_clause
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
from reranker import rerank_head, rerank_head_size
from document_read import DEFAULT_PAGE_CHARS, anchor_cursor, document_char_count, fetch_ordered_chunks
from frontmatter import frontmatter_fields_for_response
from query.meta_filters import (
    apply_tag_postfilter,
    build_metadata_where_parts,
    normalize_tag_filters,
)


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
    source: str | None = None,
    category: str | None = None,
    main_protein: str | None = None,
    min_servings: int | None = None,
    max_servings: int | None = None,
    max_total_time: int | None = None,
    tags: str | list[str] | None = None,
    course: str | None = None,
    era: str | None = None,
    rerank_status: str = "disabled",
) -> dict:
    _gc = get_chroma_client or get_client
    rerank_diag: dict[str, Any] = {"status": rerank_status}

    def _note_rerank(diag: dict[str, Any]) -> None:
        # One block per call: raw and artifact streams each rerank their own head.
        failed = rerank_diag.get("status") == "failed" or diag["status"] == "failed"
        rerank_diag.update({k: v for k, v in diag.items() if k not in ("candidates", "elapsed_ms")})
        rerank_diag["candidates"] = rerank_diag.get("candidates", 0) + diag["candidates"]
        rerank_diag["elapsed_ms"] = round(rerank_diag.get("elapsed_ms", 0.0) + diag.get("elapsed_ms", 0.0), 1)
        rerank_diag["status"] = "failed" if failed else "applied"
    ef = get_embedding_function(batch_size=1)
    client = _gc(str(db))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)

    # Resolved once per retrieval: an unscoped query must never reach a private
    # silo. Reading it here (rather than per stream) keeps the clause consistent
    # across the vector and lexical arms, which share this where filter.
    private_clause = private_filter_clause(db)
    required_tags = normalize_tag_filters(tags)
    meta_where_parts = build_metadata_where_parts(
        category=category,
        main_protein=main_protein,
        min_servings=min_servings,
        max_servings=max_servings,
        max_total_time=max_total_time,
        course=course,
        era=era,
    )

    def _where_for_silo(target_silo: str | None) -> dict | None:
        parts: list[dict[str, Any]] = []
        if target_silo:
            # Explicit silo= is the only way into a private silo. The caller named
            # it, so honor it — including when it is private.
            parts.append({"silo": target_silo})
        elif private_clause is not None:
            parts.append(private_clause)
        if doc_type:
            parts.append({"doc_type": doc_type})
        if source:
            parts.append({"source": source})
        parts.extend(meta_where_parts)
        if len(parts) == 1:
            return parts[0]
        if len(parts) > 1:
            return {"$and": parts}
        return None

    # section= is a substring match Chroma's where cannot express, so it filters each
    # stream's stage-1 pool before diversity and the n_results cut. No match means no
    # chunks: falling back to unfiltered results hands back text from other sections.
    section_lower = (section or "").strip().lower()
    section_stats = {"candidates": 0, "matched": 0}

    def _apply_rerank(
        docs_h: list[str],
        metas_h: list[dict | None],
        dists_h: list[float | None],
    ) -> tuple[list[str], list[dict | None], list[float | None]]:
        """Score a bounded head, then leave the tail in retrieval order for diversity."""
        if rerank_status != "eligible" or not docs_h:
            return docs_h, metas_h, dists_h
        docs_h, metas_h, dists_h, diag = rerank_head(
            query, docs_h, metas_h, dists_h, rerank_head_size(n_results)
        )
        _note_rerank(diag)
        return docs_h, metas_h, dists_h

    def _query_stream(
        target_silo: str | None,
    ) -> tuple[list[str], list[dict | None], list[float | None], str | None, str, dict[str, Any]]:
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
        lexical_diag: dict[str, Any] = {}
        docs_h, metas_h, dists_h, method = run_hybrid_retrieve(
            ids_v=ids_v,
            docs_v=docs_v,
            metas_v=metas_v,
            dists_v=dists_v,
            query_text=query_for_retrieval,
            collection=collection,
            where_filter=_hybrid_where,
            top_k=n_stage1,
            lexical_phrases=_lexical_phrases,
            diagnostics=lexical_diag,
        )
        if section_lower:
            section_stats["candidates"] += len(docs_h)
            kept = [
                (d, m, x) for d, m, x in zip(docs_h, metas_h, dists_h)
                if section_lower in str((m or {}).get("section") or "").lower()
            ]
            section_stats["matched"] += len(kept)
            docs_h, metas_h, dists_h = (list(col) for col in zip(*kept)) if kept else ([], [], [])
        docs_h, metas_h, dists_h = apply_tag_postfilter(docs_h, metas_h, dists_h, required_tags)
        docs_h, metas_h, dists_h = _apply_rerank(docs_h, metas_h, dists_h)
        # Dedup before the cut. Doing it after leaves a hole where the duplicate sat,
        # and the stream comes back shorter than n_results.
        docs_h, metas_h, dists_h = dedup_by_chunk_hash(docs_h, metas_h, dists_h)
        scoped = bool(target_silo or source)
        per_cap = source_diversity_cap(
            intent,
            n_results,
            silo_scoped=scoped,
            default=MAX_CHUNKS_PER_FILE,
        )
        # Unscoped calls still have a silo cap ahead. Cutting each stream to
        # n_results first leaves that cap with too few chunks to fill the request.
        stream_k = n_results if scoped else n_stage1
        docs_h, metas_h, dists_h = diversify_by_source(docs_h, metas_h, dists_h, stream_k, max_per_source=per_cap)
        return docs_h, metas_h, dists_h, _warn, method, lexical_diag

    # Report the path actually taken; this used to be a constant "hybrid_or_vector",
    # which left explain_retrieval describing hybrid runs as vector-only.
    docs, metas, dists, _silo_warning, retrieval_method, lexical_retrieval = _query_stream(silo_slug)
    if silo_slug and (doc_type is None or doc_type == "artifact") and _artifact_stream_enabled(db, silo_slug):
        artifact_slug = f"{silo_slug}-artifacts"
        artifact_docs, artifact_metas, artifact_dists, _artifact_warning, _m, _d = _query_stream(artifact_slug)
        if artifact_docs:
            docs, metas, dists = merge_dual_streams_rrf(
                docs,
                metas,
                dists,
                artifact_docs,
                artifact_metas,
                artifact_dists,
                top_k=len(docs) + len(artifact_docs),
            )
            docs, metas, dists = dedup_by_chunk_hash(docs, metas, dists)
            if silo_slug or source:
                docs, metas, dists = docs[:n_results], metas[:n_results], dists[:n_results]
            retrieval_method = "dual_stream_rrf"

    image_search: dict[str, Any] = {"attempted": False, "status": "not_applicable", "matches": 0}
    explicit_image_query = _query_explicitly_requests_image(query)
    if not section_lower and _query_is_image_relevant(query, docs, metas):
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
                    image_docs, image_metas, image_dists = dedup_by_chunk_hash(
                        image_docs, image_metas, image_dists
                    )
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
                        top_k=len(image_docs) + len(docs),
                    )
                    docs, metas, dists = dedup_by_chunk_hash(docs, metas, dists)
                    if silo_slug or source:
                        docs, metas, dists = docs[:n_results], metas[:n_results], dists[:n_results]
                    retrieval_method = f"{retrieval_method}+image_rrf"

    # source= pins one file, so there is nothing for silo balancing to spread across.
    if silo_slug is None and not source:
        per_silo_cap = n_results if explicit_image_query and image_search["status"] == "ok" else max_silo_chunks_for_intent(intent, 3)
        silo_cache = [str(((m or {}).get("silo") or "")) for m in metas]
        docs, metas, dists = diversify_by_silo(
            docs, metas, dists, n_results, max_per_silo=per_silo_cap, silos=silo_cache
        )

    # A named file that fits in one read_document page comes back in document
    # order, whole. Ranked chunks of a two-paragraph note hide the note.
    whole_file = False
    source_file = None
    if source and docs:
        file_silo = silo_slug or str((metas[0] or {}).get("silo") or "")
        file_source = str((metas[0] or {}).get("source") or "")
        if file_silo and file_source:
            try:
                ordered = fetch_ordered_chunks(collection, silo=file_silo, source=file_source)
            except Exception:
                ordered = []
            if ordered:
                file_chars = document_char_count(ordered)
                source_file = {"total_chunks": len(ordered), "total_chars": file_chars}
                if file_chars <= DEFAULT_PAGE_CHARS:
                    distance_by_hash: dict[str, float | None] = {}
                    for meta, dist in zip(metas, dists):
                        hashed = str((meta or {}).get("chunk_hash") or "")
                        if hashed and hashed not in distance_by_hash:
                            distance_by_hash[hashed] = dist
                    docs = [doc for doc, _meta in ordered]
                    metas = [meta for _doc, meta in ordered]
                    dists = [
                        distance_by_hash.get(str((meta or {}).get("chunk_hash") or ""))
                        for meta in metas
                    ]
                    whole_file = True
                    retrieval_method = "whole_file"

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
            "record_type": m.get("record_type"),
            "source_modality": m.get("source_modality"),
            "summary_status": m.get("summary_status"),
            "needs_vision_enrichment": m.get("needs_vision_enrichment"),
            "indexed_at": m.get("indexed_at"),
            "_signals": signals,
        }
        if m.get("chunk_index") is not None:
            chunk["chunk_index"] = m.get("chunk_index")
        fm_fields = frontmatter_fields_for_response(m)
        if fm_fields:
            chunk["metadata"] = fm_fields
        if photo_metadata:
            chunk["photo_metadata"] = photo_metadata
        if m.get("chunk_hash"):
            chunk["read_cursor"] = anchor_cursor(str(m["chunk_hash"]))
        chunks.append(chunk)

    result: dict = {
        "query": query,
        "intent": intent,
        "silo_filter": silo_slug,
        "retrieval_method": retrieval_method,
        "lexical_retrieval": lexical_retrieval,
        "rerank": rerank_diag,
        "image_search": image_search,
        "chunks": chunks,
    }
    if source_file is not None:
        result["source_file"] = source_file
    if whole_file:
        result["whole_file"] = True
    visual_follow_up = [
        str(chunk.get("source") or "")
        for chunk in chunks
        if chunk.get("source_modality") == "image"
        and chunk.get("summary_status") in {"deferred", "disabled"}
        and chunk.get("source")
    ]
    if visual_follow_up:
        # Ranked best first. On 2026-10-02 the caller got three unranked files,
        # asked about the third, and gave up; ask_image now takes them together.
        ranked = list(dict.fromkeys(visual_follow_up))[:3]
        result["recommended_action"] = {
            "tool": "ask_image",
            "args": {"files": ranked, "question": query},
            "reason": (
                "The matched images have no visual description. Pass all of these files "
                "(best match first) to ask_image in one call before answering visual details."
            ),
            "files": ranked,
        }
    if _silo_warning:
        result["silo_warning"] = _silo_warning
    if section_lower and not section_stats["matched"]:
        result["section_filter"] = {"section": section, **section_stats}

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
