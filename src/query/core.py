"""
Core query orchestration: run_ask() delegates to query.ask; run_retrieve() for MCP-style chunk lists.
Symbols from query.core_reexports are exposed here for execute_run_ask and for monkeypatch(query.core.*) tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from ingest import get_paths_by_silo, _read_image_artifact, _update_image_artifact
except ImportError:
    get_paths_by_silo = None  # type: ignore[misc, assignment]
    _read_image_artifact = None  # type: ignore[misc, assignment]
    _update_image_artifact = None  # type: ignore[misc, assignment]

from processors import _summarize_image_with_vision_model

try:
    from state import get_silo_image_vision_enabled
except ImportError:
    get_silo_image_vision_enabled = None  # type: ignore[misc, assignment]

from query.core_reexports import *  # noqa: F403

_DETERMINISTIC_INTENTS = frozenset({
    INTENT_CAPABILITIES,
    INTENT_CODE_LANGUAGE,
    INTENT_STRUCTURE,
    INTENT_FILE_LIST,
    INTENT_TIMELINE,
    INTENT_METADATA_ONLY,
})


def run_ask(
    archetype_id: str | None,
    query: str,
    config_path: str | Path | None = None,
    n_results: int = DEFAULT_N_RESULTS,
    model: str = DEFAULT_MODEL,
    no_color: bool = False,
    use_reranker: bool | None = None,
    silo: str | None = None,
    db_path: str | Path | None = None,
    strict: bool = False,
    quiet: bool = False,
    explain: bool = False,
    force: bool = False,
    explicit_unified: bool = False,
    get_chroma_client: Any | None = None,
) -> str:
    """Query archetype's collection, or unified llmli collection if archetype_id is None (optional silo filter)."""
    from query.ask.orchestrator import execute_run_ask

    return execute_run_ask(
        archetype_id,
        query,
        config_path=config_path,
        n_results=n_results,
        model=model,
        no_color=no_color,
        use_reranker=use_reranker,
        silo=silo,
        db_path=db_path,
        strict=strict,
        quiet=quiet,
        explain=explain,
        force=force,
        explicit_unified=explicit_unified,
        get_chroma_client=get_chroma_client or get_client,
    )


def run_retrieve(
    query: str,
    silo: str | None = None,
    n_results: int = DEFAULT_N_RESULTS,
    section: str | None = None,
    doc_type: str | None = None,
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
    get_chroma_client: Any | None = None,
) -> dict:
    """
    Return raw retrieved chunks with metadata — no LLM synthesis.

    Runs intent routing, query expansion, vector retrieval, diversification,
    and deduplication, then returns the chunk list for the caller to reason over.
    Pass section= to post-filter chunks to a specific document section (e.g. 'Item 1A').
    Pass doc_type= to restrict to a specific document type stored in chunk metadata
    (e.g. 'transcript', 'resume', 'pdf', 'code', 'other').
    Deterministic intents (CAPABILITIES, CODE_LANGUAGE, STRUCTURE, etc.) bypass
    vector retrieval and return a note field with the answer.
    """
    db = str(db_path or DB_PATH)
    intent = route_intent(query)

    if intent in _DETERMINISTIC_INTENTS:
        return {
            "query": query,
            "intent": intent,
            "silo_filter": silo,
            "note": (
                f"Intent '{intent}' is deterministic and does not use vector retrieval. "
                "Try rephrasing as a descriptive question for semantic retrieval."
            ),
            "chunks": [],
        }

    silo_slug: str | None = None
    if silo:
        silo_slug = resolve_silo_to_slug(db, silo) or silo

    use_reranker = is_reranker_enabled()
    n_effective = effective_k(intent, n_results)
    if intent in (INTENT_EVIDENCE_PROFILE, INTENT_AGGREGATE, INTENT_ACADEMIC_HISTORY):
        n_stage1 = max(n_effective, RERANK_STAGE1_N if use_reranker else 60)
    elif intent == INTENT_REFLECT:
        n_stage1 = n_effective
    else:
        n_stage1 = RERANK_STAGE1_N if use_reranker else min(100, max(n_results * 5, 60))

    query_for_retrieval = query.strip()
    if intent not in (INTENT_FIELD_LOOKUP, INTENT_CAPABILITIES, INTENT_CODE_LANGUAGE):
        query_for_retrieval = expand_query(query_for_retrieval)

    from chroma_lock import chroma_shared_lock
    from query.retrieve_locked import execute_retrieve_chroma_phase

    _gc = get_chroma_client or get_client
    with chroma_shared_lock(str(db)):
        return execute_retrieve_chroma_phase(
            db=db,
            intent=intent,
            query=query,
            query_for_retrieval=query_for_retrieval,
            silo_slug=silo_slug,
            n_stage1=n_stage1,
            n_results=n_results,
            section=section,
            doc_type=doc_type,
            db_path=str(db_path) if db_path is not None else None,
            get_chroma_client=_gc,
        )


def main() -> None:
    """CLI entry: librarian.py <archetype_id> <query> (used by cli.py ask)."""
    if len(sys.argv) < 3:
        print("Usage: python librarian.py <archetype_id> <query>")
        sys.exit(1)
    archetype_id = sys.argv[1]
    query = " ".join(sys.argv[2:])
    print(run_ask(archetype_id, query))
