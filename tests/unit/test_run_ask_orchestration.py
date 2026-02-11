from query.intent import (
    INTENT_CAPABILITIES,
    INTENT_CODE_LANGUAGE,
    INTENT_EVIDENCE_PROFILE,
    INTENT_FIELD_LOOKUP,
    INTENT_FILE_LIST,
    INTENT_LOOKUP,
)
from query.core import QueryPolicyError, run_ask


class _DummyClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _patch_query_runtime(monkeypatch, mock_collection):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))


def test_run_ask_capabilities_bypasses_llm(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_CAPABILITIES)
    out = run_ask(archetype_id=None, query="what can you index?", no_color=True, use_reranker=False)
    assert "Supported file extensions" in out
    assert mock_ollama["calls"] == []


def test_run_ask_file_list_short_circuits_without_retrieval_or_llm(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FILE_LIST)
    monkeypatch.setattr("query.core.parse_file_list_year_request", lambda _q: {"year": "2022"})
    monkeypatch.setattr("query.core.validate_catalog_freshness", lambda _db, _silo: {"stale": False, "stale_reason": None, "scanned_count": 3})
    monkeypatch.setattr(
        "query.core.list_files_from_year",
        lambda _db, _silo, _year, year_mode="mtime", cap=50: {
            "files": ["/tmp/a.txt", "/tmp/b.txt"],
            "scanned_count": 3,
            "matched_count": 2,
            "cap_applied": False,
            "match_reason_counts": {"mtime_year": 2, "path_year_token": 0},
            "scope": "silo:stuff-12345678",
            "stale": False,
            "stale_reason": None,
        },
    )
    monkeypatch.setattr(
        "query.core.chromadb.PersistentClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not create client for FILE_LIST")),
    )
    out = run_ask(
        archetype_id=None,
        query="what files are from 2022",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-12345678",
    )
    assert out == "/tmp/a.txt\n/tmp/b.txt"
    assert mock_ollama["calls"] == []


def test_run_ask_file_list_requires_explicit_silo(monkeypatch):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FILE_LIST)
    monkeypatch.setattr("query.core.parse_file_list_year_request", lambda _q: {"year": "2022"})
    try:
        run_ask(
            archetype_id=None,
            query="what files are from 2022",
            no_color=True,
            use_reranker=False,
            quiet=True,
            silo=None,
        )
        assert False, "expected QueryPolicyError"
    except QueryPolicyError as e:
        assert "require explicit scope" in str(e).lower()


def test_run_ask_file_list_stale_scope_fails_closed(monkeypatch):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FILE_LIST)
    monkeypatch.setattr("query.core.parse_file_list_year_request", lambda _q: {"year": "2022"})
    monkeypatch.setattr(
        "query.core.validate_catalog_freshness",
        lambda _db, _silo: {"stale": True, "stale_reason": "file_changed_since_index", "scanned_count": 2},
    )
    try:
        run_ask(
            archetype_id=None,
            query="what files are from 2022",
            no_color=True,
            use_reranker=False,
            quiet=True,
            silo="stuff-12345678",
            force=False,
        )
        assert False, "expected QueryPolicyError"
    except QueryPolicyError as e:
        assert "stale" in str(e).lower()
        assert e.exit_code == 2


def test_run_ask_file_list_strict_toggle_no_effect(monkeypatch):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FILE_LIST)
    monkeypatch.setattr("query.core.parse_file_list_year_request", lambda _q: {"year": "2022"})
    monkeypatch.setattr("query.core.validate_catalog_freshness", lambda _db, _silo: {"stale": False, "stale_reason": None, "scanned_count": 3})
    monkeypatch.setattr(
        "query.core.list_files_from_year",
        lambda _db, _silo, _year, year_mode="mtime", cap=50: {
            "files": ["/tmp/a.txt"],
            "scanned_count": 3,
            "matched_count": 1,
            "cap_applied": False,
            "match_reason_counts": {"mtime_year": 1, "path_year_token": 0},
            "scope": "silo:stuff-12345678",
            "stale": False,
            "stale_reason": None,
        },
    )
    out_loose = run_ask(
        archetype_id=None,
        query="what files are from 2022",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-12345678",
        strict=False,
    )
    out_strict = run_ask(
        archetype_id=None,
        query="what files are from 2022",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-12345678",
        strict=True,
    )
    assert out_loose == out_strict == "/tmp/a.txt"


def test_run_ask_code_language_bypasses_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_CODE_LANGUAGE)
    monkeypatch.setattr(
        "query.core.get_code_language_stats_from_registry",
        lambda _db, _silo: ({".py": 3}, {".py": ["a.py"]}),
    )
    monkeypatch.setattr(
        "query.core.format_code_language_answer",
        lambda by_ext, _sample, _source, _no_color: f"Top: {next(iter(by_ext.keys()))}",
    )
    out = run_ask(archetype_id=None, query="most common language?", no_color=True, use_reranker=False)
    assert "Top: .py" in out
    assert mock_ollama["calls"] == []


def test_run_ask_lookup_calls_ollama(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "s1"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    out = run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False)
    assert "Sources:" in out
    assert len(mock_ollama["calls"]) == 1
    messages = mock_ollama["calls"][0]["messages"]
    assert "[START CONTEXT]" in messages[1]["content"]


def test_run_ask_applies_query_expansion_for_lookup(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.expand_query", lambda q: f"{q} wages salary")
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "s1"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    run_ask(archetype_id=None, query="income details", no_color=True, use_reranker=False)
    q_calls = [kwargs for name, kwargs in mock_collection.calls if name == "query"]
    assert q_calls
    assert q_calls[0]["query_texts"] == ["income details wages salary"]


def test_run_ask_applies_silo_filter(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["x"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    run_ask(
        archetype_id=None,
        query="what",
        no_color=True,
        use_reranker=False,
        silo="tax-1234",
    )
    q_calls = [kwargs for name, kwargs in mock_collection.calls if name == "query"]
    assert q_calls
    assert q_calls[0]["where"] == {"silo": "tax-1234"}
    assert len(mock_ollama["calls"]) == 1


def test_run_ask_relevance_gate_skips_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["irrelevant"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[9.9]],
        "ids": [["id-1"]],
    }
    out = run_ask(archetype_id=None, query="needle", no_color=True, use_reranker=False)
    assert "I don't have relevant content for that." in out
    assert mock_ollama["calls"] == []


def test_run_ask_quiet_omits_footer_and_sources(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "s1"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {"message": {"content": "final answer"}}
    out = run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False, quiet=True)
    assert out == "final answer"
    assert "Sources:" not in out


def test_run_ask_adds_confidence_warning_when_single_source(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "s1"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    out = run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False)
    assert "Single source: answer is based on one document only." in out


def test_run_ask_strict_mode_adds_strict_instruction(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["strict context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    run_ask(archetype_id=None, query="list all", no_color=True, use_reranker=False, strict=True)
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert "Strict mode:" in system_prompt


def test_run_ask_direct_decisive_mode_adds_conflict_policy(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {}, "query": {"direct_decisive_mode": True}},
    )
    mock_collection.query_result = {
        "documents": [["canonical context"]],
        "metadatas": [[{"source": "/tmp/2025-canonical-fact.md", "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_collection.get_result = {"ids": [], "documents": [], "metadatas": []}

    run_ask(archetype_id=None, query="what is the canonical fact", no_color=True, use_reranker=False)
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert "Direct query conflict policy" in system_prompt


def test_run_ask_strict_mode_not_replaced_by_direct_mode_when_disabled(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {}, "query": {"direct_decisive_mode": False}},
    )
    mock_collection.query_result = {
        "documents": [["strict context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_collection.get_result = {"ids": [], "documents": [], "metadatas": []}

    run_ask(archetype_id=None, query="list all", no_color=True, use_reranker=False, strict=True)
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert "Strict mode:" in system_prompt
    assert "Direct query conflict policy" not in system_prompt


def test_run_ask_direct_decisive_relaxes_low_confidence_when_canonical_present(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {
            "archetypes": {},
            "query": {
                "direct_decisive_mode": True,
                "canonical_filename_tokens": ["canonical"],
                "deprioritized_tokens": ["draft"],
                "confidence_relaxation_enabled": True,
            },
        },
    )
    mock_collection.query_result = {
        "documents": [["draft context", "canonical context"]],
        "metadatas": [[
            {"source": "/tmp/draft-note.md", "is_local": 1},
            {"source": "/tmp/official-canonical-fact.md", "is_local": 1},
        ]],
        "distances": [[0.95, 0.91]],
        "ids": [["id-1", "id-2"]],
    }
    mock_collection.get_result = {
        "ids": ["id-2"],
        "documents": ["canonical context"],
        "metadatas": [{"source": "/tmp/official-canonical-fact.md", "is_local": 1}],
    }
    out = run_ask(archetype_id=None, query="what is the fact", no_color=True, use_reranker=False)
    assert "Low confidence: query is weakly related to indexed content." not in out


def test_run_ask_silo_prompt_override_precedence(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: "Override prompt.")
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: "Stuff")
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {"stuff": {"name": "Stuff", "prompt": "Config prompt."}}},
    )
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "stuff-deadbeef"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }

    run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False, silo="stuff-deadbeef")
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert system_prompt.startswith("Override prompt.")


def test_run_ask_silo_prompt_falls_back_to_slug_base(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: None)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {"stuff": {"name": "Stuff", "prompt": "Hash base prompt."}}},
    )
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "stuff-deadbeef"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }

    run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False, silo="stuff-deadbeef")
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert system_prompt.startswith("Hash base prompt.")


def test_run_ask_silo_prompt_falls_back_to_normalized_display_name(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: "My Stuff")
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {"my-stuff": {"name": "My Stuff", "prompt": "Display prompt."}}},
    )
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "x-12345678"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }

    run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False, silo="x-12345678")
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert system_prompt.startswith("Display prompt.")


def test_run_ask_silo_prompt_uses_default_when_no_override_or_archetype(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}})
    mock_collection.query_result = {
        "documents": [["alpha context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "x-12345678"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }

    run_ask(archetype_id=None, query="what is alpha", no_color=True, use_reranker=False, silo="x-12345678")
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert system_prompt.startswith("Answer only from the provided context. Be concise.")


def test_run_ask_evidence_profile_uses_hybrid_get(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_EVIDENCE_PROFILE)
    mock_collection.query_result = {
        "documents": [["I like apples"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1, "silo": "stuff"}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_collection.get_result = {
        "ids": ["id-1"],
        "documents": ["I like apples"],
        "metadatas": [{"source": "/tmp/a.txt", "is_local": 1, "silo": "stuff"}],
    }
    run_ask(archetype_id=None, query="what do i like", no_color=True, use_reranker=False, silo="stuff")
    assert any(name == "get" for name, _kwargs in mock_collection.calls)
    assert len(mock_ollama["calls"]) == 1


def test_run_ask_measurement_intent_without_timing_short_circuits(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["general notes, no timing metrics here"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    out = run_ask(
        archetype_id=None,
        query="what's the latency",
        no_color=True,
        use_reranker=False,
    )
    assert "can't find performance traces" in out
    assert mock_ollama["calls"] == []


def test_run_ask_uses_subscope_where_when_resolved(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.resolve_subscope",
        lambda _query, _db, _cb: (["silo-a"], ["/tmp/a.txt"], ["a"]),
    )
    mock_collection.query_result = {
        "documents": [["x"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    run_ask(archetype_id=None, query="why is a fast", no_color=True, use_reranker=False)
    q_calls = [kwargs for name, kwargs in mock_collection.calls if name == "query"]
    assert q_calls
    assert q_calls[0]["where"] == {"$and": [{"silo": {"$in": ["silo-a"]}}, {"source": {"$in": ["/tmp/a.txt"]}}]}
    assert len(mock_ollama["calls"]) == 1


def test_run_ask_field_lookup_year_not_indexed_returns_guardrail(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FIELD_LOOKUP)
    mock_collection.get_result = {"ids": [], "documents": [], "metadatas": []}

    out = run_ask(
        archetype_id=None,
        query="on 2024 form 1040 what is line 9 total income",
        no_color=True,
        use_reranker=False,
        silo="tax-123",
    )
    assert "I could not find indexed tax documents for 2024 in this silo." in out
    assert mock_ollama["calls"] == []
    assert not any(name == "query" for name, _kwargs in mock_collection.calls)


def test_run_ask_field_lookup_missing_line_returns_guardrail(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FIELD_LOOKUP)
    mock_collection.get_result = {
        "ids": ["id-1"],
        "documents": ["Form 1040 (2024)\nline 10: 123"],
        "metadatas": [{"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf", "silo": "tax-123"}],
    }

    out = run_ask(
        archetype_id=None,
        query="on 2024 form 1040 what is line 9 total income",
        no_color=True,
        use_reranker=False,
        silo="tax-123",
    )
    assert "I found 2024 tax documents, but I could not find Form 1040 line 9 in extractable text." in out
    assert "I'm not inferring from other years." in out
    assert mock_ollama["calls"] == []


def test_run_ask_field_lookup_exact_match_is_value_first(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_FIELD_LOOKUP)
    mock_collection.get_result = {
        "ids": ["id-1", "id-2"],
        "documents": [
            "Form 1040 (2021)\nline 9: 99,999.",
            "Form 1040 (2024)\nStructured values:\nline 9: 7,522.\n",
        ],
        "metadatas": [
            {"source": "/Users/x/Tax/2021/2021_TaxReturn.pdf", "silo": "tax-123"},
            {"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf", "silo": "tax-123"},
        ],
    }

    out = run_ask(
        archetype_id=None,
        query="on 2024 form 1040 what is line 9 total income",
        no_color=True,
        use_reranker=False,
        silo="tax-123",
    )
    assert "Form 1040 line 9 (2024): 7,522." in out
    assert "2024 Federal Income Tax Return.pdf" in out
    assert mock_ollama["calls"] == []


def test_run_ask_csv_rank_guardrail_short_circuits_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.get_result = {
        "documents": [
            'CSV row 1: Rank=1 | Restaurant=Carmine\'s (Times Square) | Sales=39080335',
            "CSV row 2: Rank=2 | Restaurant=The Boathouse Orlando | Sales=35218364",
        ],
        "metadatas": [
            {"source": "/tmp/2020/Independence100.csv", "silo": "restaurant"},
            {"source": "/tmp/2020/Independence100.csv", "silo": "restaurant"},
        ],
        "ids": ["id-1", "id-2"],
    }

    out = run_ask(
        archetype_id=None,
        query="what restaurant was ranked number 1 in 2020",
        no_color=True,
        use_reranker=False,
        silo="restaurant",
    )
    assert "Rank 1: Carmine's (Times Square)" in out
    assert mock_ollama["calls"] == []


def test_run_ask_csv_rank_guardrail_works_in_strict_mode(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.get_result = {
        "documents": ['CSV row 1: Rank=1 | Restaurant=Carmine\'s (Times Square) | Sales=39080335'],
        "metadatas": [{"source": "/tmp/2020/Independence100.csv", "silo": "restaurant"}],
        "ids": ["id-1"],
    }

    out = run_ask(
        archetype_id=None,
        query="what restaurant was ranked number 1 in 2020",
        no_color=True,
        use_reranker=False,
        silo="restaurant",
        strict=True,
    )
    assert "Rank 1: Carmine's (Times Square)" in out
    assert mock_ollama["calls"] == []


def test_run_ask_direct_value_guardrail_prefers_canonical_and_skips_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [[
            "Aster Grill operating margin: 14.9%",
            "Aster Grill operating margin: 18.2%",
        ]],
        "metadatas": [[
            {"source": "/tmp/2025-04-06-margin-contradiction.md", "is_local": 1},
            {"source": "/tmp/2025-04-05-canonical-margin.md", "is_local": 1},
        ]],
        "distances": [[0.1, 0.11]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(
        archetype_id=None,
        query="What is Aster Grill operating margin?",
        no_color=True,
        use_reranker=False,
    )
    assert "operating margin: 18.2%" in out
    assert mock_ollama["calls"] == []


def test_run_ask_direct_value_guardrail_abstains_on_equal_conflict(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [[
            "Fjord Bistro monthly churn: 4.2%",
            "Fjord Bistro monthly churn: 6.8%",
        ]],
        "metadatas": [[
            {"source": "/tmp/2025-05-15-equal-conflict-a.md", "is_local": 1},
            {"source": "/tmp/2025-05-15-equal-conflict-b.md", "is_local": 1},
        ]],
        "distances": [[0.1, 0.1]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(
        archetype_id=None,
        query="What's the exact Fjord Bistro churn?",
        no_color=True,
        use_reranker=False,
    )
    assert "don't have enough evidence" in out.lower()
    assert mock_ollama["calls"] == []


def test_run_ask_direct_value_guardrail_same_with_strict_toggle(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["Aster Grill operating margin: 18.2%"]],
        "metadatas": [[{"source": "/tmp/2025-04-05-canonical-margin.md", "is_local": 1}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    out_loose = run_ask(
        archetype_id=None,
        query="What is Aster Grill operating margin?",
        no_color=True,
        use_reranker=False,
        strict=False,
    )
    out_strict = run_ask(
        archetype_id=None,
        query="What is Aster Grill operating margin?",
        no_color=True,
        use_reranker=False,
        strict=True,
    )
    assert "operating margin: 18.2%" in out_loose
    assert "operating margin: 18.2%" in out_strict
    assert len(mock_ollama["calls"]) == 0
