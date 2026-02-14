from query.guardrails import field_lookup_candidates_from_scope
from query.retrieval import (
    all_dists_above_threshold,
    dedup_by_chunk_hash,
    diversify_by_source,
    max_chunks_for_intent,
    relevance_max_distance,
    extract_scope_tokens,
    resolve_subscope,
    rrf_merge,
    extract_direct_lexical_terms,
    sort_by_source_priority,
    source_extension_rank_map,
)


def test_rrf_merge_keeps_vector_and_adds_lexical_only_ids():
    docs, metas, dists = rrf_merge(
        ids_v=["v1", "v2"],
        docs_v=["doc-v1", "doc-v2"],
        metas_v=[{"source": "a"}, {"source": "b"}],
        dists_v=[0.1, 0.2],
        ids_l=["v2", "l3"],
        docs_l=["doc-v2-lex", "doc-l3"],
        metas_l=[{"source": "b"}, {"source": "c"}],
        top_k=3,
    )
    assert len(docs) == 3
    assert "doc-l3" in docs
    assert any(d is None for d in dists)
    assert any((m or {}).get("source") == "c" for m in metas)


def test_diversify_by_source_caps_per_source_with_precomputed_sources():
    docs, metas, dists = diversify_by_source(
        docs=["a1", "a2", "a3", "b1", "b2"],
        metas=[{"source": "A"}, {"source": "A"}, {"source": "A"}, {"source": "B"}, {"source": "B"}],
        dists=[0.1, 0.2, 0.3, 0.4, 0.5],
        top_k=5,
        max_per_source=2,
        sources=["A", "A", "A", "B", "B"],
    )
    assert len(docs) == 4
    assert [m.get("source") for m in metas if m] == ["A", "A", "B", "B"]
    assert dists == [0.1, 0.2, 0.4, 0.5]


def test_diversify_by_source_handles_empty_docs():
    docs, metas, dists = diversify_by_source([], [], [], top_k=3)
    assert docs == []
    assert metas == []
    assert dists == []


def test_max_chunks_for_intent_uses_intent_specific_caps():
    assert max_chunks_for_intent("LOOKUP", 4) == 3
    assert max_chunks_for_intent("EVIDENCE_PROFILE", 4) == 2
    assert max_chunks_for_intent("AGGREGATE", 4) == 6
    assert max_chunks_for_intent("unknown", 4) == 4


def test_dedup_by_chunk_hash_noop_when_env_disabled(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_DEDUP_CHUNK_HASH", raising=False)
    docs = ["x1", "x2"]
    metas = [{"chunk_hash": "h1"}, {"chunk_hash": "h1"}]
    dists = [0.1, 0.2]
    out_docs, out_metas, out_dists = dedup_by_chunk_hash(docs, metas, dists)
    assert out_docs == docs
    assert out_metas == metas
    assert out_dists == dists


def test_dedup_by_chunk_hash_filters_duplicates_when_enabled(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_DEDUP_CHUNK_HASH", "1")
    out_docs, out_metas, out_dists = dedup_by_chunk_hash(
        docs=["first", "dup", "nohash", "unique"],
        metas=[
            {"chunk_hash": "h1"},
            {"chunk_hash": "h1"},
            {},
            {"chunk_hash": "h2"},
        ],
        dists=[0.1, 0.2, 0.3, 0.4],
    )
    assert out_docs == ["first", "nohash", "unique"]
    assert [m.get("chunk_hash") for m in out_metas] == ["h1", None, "h2"]
    assert out_dists == [0.1, 0.3, 0.4]


def test_extract_scope_tokens_prefers_named_tool_pattern():
    tokens = extract_scope_tokens("why is the pal tool so fast?")
    assert "pal" in tokens
    assert len(tokens) <= 2


def test_extract_scope_tokens_applies_stoplist_and_fallback_limit():
    tokens = extract_scope_tokens("why is the tool fast and how does sentinelzero feel")
    assert "tool" not in tokens
    assert len(tokens) <= 2


def test_resolve_subscope_returns_none_when_no_tokens():
    assert resolve_subscope("", "/tmp/db", lambda _db: {"silo": {"/x"}}) is None


def test_resolve_subscope_returns_matching_silos_and_paths():
    def _get_paths_by_silo(_db):
        return {
            "pal": {"/Users/a/pal/README.md", "/Users/a/pal/src/main.py"},
            "other": {"/Users/a/notes/todo.md"},
        }

    result = resolve_subscope("why is pal fast", "/tmp/db", _get_paths_by_silo)
    assert result is not None
    silos, paths, tokens = result
    assert "pal" in silos
    assert any("/pal/" in p for p in paths)
    assert tokens


def test_resolve_subscope_returns_none_when_no_matches():
    def _get_paths_by_silo(_db):
        return {"alpha": {"/tmp/alpha.txt"}}

    assert resolve_subscope("query about beta", "/tmp/db", _get_paths_by_silo) is None


def test_all_dists_above_threshold_true_only_when_all_non_none_are_above():
    assert all_dists_above_threshold([2.1, 2.2, None], 2.0) is True
    assert all_dists_above_threshold([2.1, 1.8], 2.0) is False
    assert all_dists_above_threshold([None, None], 2.0) is False


def test_relevance_max_distance_uses_tighter_default(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE", raising=False)
    assert relevance_max_distance() == 0.9


def test_relevance_max_distance_allows_env_override(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_RELEVANCE_MAX_DISTANCE", "0.72")
    assert relevance_max_distance() == 0.72


def test_extract_direct_lexical_terms_keeps_entity_and_numeric_anchors():
    terms = extract_direct_lexical_terms("What is Aster Grill revenue rank in 2025?")
    assert "aster" in terms
    assert "grill" in terms
    assert "2025" in terms


def test_sort_by_source_priority_promotes_canonical_sources():
    docs, metas, dists = sort_by_source_priority(
        docs=["draft answer", "canonical answer"],
        metas=[{"source": "2025-04-10-draft-note.md"}, {"source": "2025-04-10-canonical-hours.md"}],
        dists=[0.1, 0.2],
        canonical_tokens=["canonical", "official"],
        deprioritized_tokens=["draft", "archive"],
    )
    assert docs[0] == "canonical answer"
    assert (metas[0] or {}).get("source", "").endswith("canonical-hours.md")


def test_source_extension_rank_map_prefers_extension_at_source_level():
    metas = [
        {"source": "/tmp/notes.docx"},
        {"source": "/tmp/deck.pptx"},
        {"source": "/tmp/deck.pptx"},
    ]
    dists = [0.05, 0.30, 0.40]
    rank_map = source_extension_rank_map(metas, dists, [".pptx", ".ppt"])
    assert rank_map["/tmp/deck.pptx"] < rank_map["/tmp/notes.docx"]


def test_source_extension_rank_map_empty_without_hints():
    rank_map = source_extension_rank_map([{"source": "/tmp/a.txt"}], [0.1], [])
    assert rank_map == {}


def test_field_lookup_candidates_do_not_fallback_across_years():
    docs = [
        "Form 1040\nline 9: 99,999.",
        "Form 1040\nline 9: 7,522.",
    ]
    metas = [
        {"source": "/Users/x/Tax/2021/2021_TaxReturn.pdf"},
        {"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf"},
    ]
    year_docs, year_form_docs = field_lookup_candidates_from_scope(docs, metas, year="2024", form="1040")
    assert len(year_docs) == 1
    assert len(year_form_docs) == 1
    assert "7,522." in year_form_docs[0][0]
