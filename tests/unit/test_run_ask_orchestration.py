import json

from query.intent import (
    INTENT_CAPABILITIES,
    INTENT_CODE_LANGUAGE,
    INTENT_EVIDENCE_PROFILE,
    INTENT_FIELD_LOOKUP,
    INTENT_FILE_LIST,
    INTENT_MONEY_YEAR_TOTAL,
    INTENT_TAX_QUERY,
    INTENT_STRUCTURE,
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


def test_run_ask_structure_short_circuits_without_retrieval(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "outline", "wants_summary": False})
    monkeypatch.setattr(
        "query.core.build_structure_outline",
        lambda _db, _silo, cap=200: {
            "mode": "outline",
            "lines": ["slides/APT Simulation and Analysis.pptx", "notes/summary.md"],
            "scanned_count": 10,
            "matched_count": 2,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": False,
            "stale_reason": None,
        },
    )
    monkeypatch.setattr("query.core.validate_catalog_freshness", lambda _db, _silo: {"stale": False, "stale_reason": None, "scanned_count": 10})
    monkeypatch.setattr(
        "query.core.chromadb.PersistentClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("structure path should not create chroma client")),
    )
    out = run_ask(
        archetype_id=None,
        query="show structure snapshot",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-deadbeef",
    )
    assert out == "slides/APT Simulation and Analysis.pptx\nnotes/summary.md"
    assert mock_ollama["calls"] == []


def test_run_ask_structure_without_scope_returns_deterministic_guidance(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "outline", "wants_summary": False})
    monkeypatch.setattr(
        "query.core.rank_scope_candidates",
        lambda _q, _db, top_n=3: [
            {"slug": "stuff-deadbeef", "display_name": "Stuff", "score": 5.0, "matched_tokens": ["stuff"]},
            {"slug": "school-12345678", "display_name": "School", "score": 3.0, "matched_tokens": ["school"]},
        ],
    )
    out = run_ask(
        archetype_id=None,
        query="show structure",
        no_color=True,
        use_reranker=False,
        quiet=False,
        silo=None,
    )
    assert "No scope selected." in out
    assert "Stuff (stuff-deadbeef)" in out
    assert mock_ollama["calls"] == []


def test_run_ask_structure_without_scope_quiet_returns_single_line_policy_error(monkeypatch):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "outline", "wants_summary": False, "ext": None})
    try:
        run_ask(
            archetype_id=None,
            query="show structure",
            no_color=True,
            use_reranker=False,
            quiet=True,
            silo=None,
        )
        assert False, "expected QueryPolicyError"
    except QueryPolicyError as e:
        assert str(e) == 'No scope selected. Try: pal ask --in <silo> "show structure"'
        assert e.exit_code == 2


def test_run_ask_structure_stale_warns_but_returns_results(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "recent", "wants_summary": False})
    monkeypatch.setattr(
        "query.core.build_structure_recent",
        lambda _db, _silo, cap=100: {
            "mode": "recent",
            "lines": ["2024-05-01 notes/a.md"],
            "scanned_count": 5,
            "matched_count": 1,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": False,
            "stale_reason": None,
        },
    )
    monkeypatch.setattr(
        "query.core.validate_catalog_freshness",
        lambda _db, _silo: {"stale": True, "stale_reason": "file_changed_since_index", "scanned_count": 5},
    )
    out = run_ask(
        archetype_id=None,
        query="recent changes",
        no_color=True,
        use_reranker=False,
        quiet=False,
        silo="stuff-deadbeef",
    )
    assert out.startswith("⚠ Index may be outdated")
    assert "2024-05-01 notes/a.md" in out
    assert mock_ollama["calls"] == []


def test_run_ask_structure_extension_count_short_circuits_llm(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "ext_count", "wants_summary": False, "ext": ".docx"})
    monkeypatch.setattr(
        "query.core.build_structure_extension_count",
        lambda _db, _silo, _ext: {
            "mode": "ext_count",
            "ext": ".docx",
            "count": 197,
            "scanned_count": 519,
            "scope": "silo:stuff-bf5fc7e8",
            "stale": False,
            "stale_reason": None,
        },
    )
    monkeypatch.setattr("query.core.validate_catalog_freshness", lambda _db, _silo: {"stale": False, "stale_reason": None, "scanned_count": 519})
    monkeypatch.setattr(
        "query.core.chromadb.PersistentClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ext_count should not create chroma client")),
    )
    out_quiet = run_ask(
        archetype_id=None,
        query="how many .docx files are there",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-bf5fc7e8",
    )
    out = run_ask(
        archetype_id=None,
        query="how many .docx files are there",
        no_color=True,
        use_reranker=False,
        quiet=False,
        silo="stuff-bf5fc7e8",
    )
    assert out_quiet == "197"
    assert "There are 197 .docx file(s)" in out
    assert mock_ollama["calls"] == []


def test_run_ask_structure_strict_toggle_no_effect(monkeypatch):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_STRUCTURE)
    monkeypatch.setattr("query.core.parse_structure_request", lambda _q: {"mode": "inventory", "wants_summary": False})
    monkeypatch.setattr(
        "query.core.build_structure_inventory",
        lambda _db, _silo, cap=200: {
            "mode": "inventory",
            "lines": [".md 2", ".pdf 1"],
            "scanned_count": 3,
            "matched_count": 2,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": False,
            "stale_reason": None,
        },
    )
    monkeypatch.setattr("query.core.validate_catalog_freshness", lambda _db, _silo: {"stale": False, "stale_reason": None, "scanned_count": 3})
    out_loose = run_ask(
        archetype_id=None,
        query="file type inventory",
        no_color=True,
        use_reranker=False,
        quiet=True,
        strict=False,
        silo="stuff-deadbeef",
    )
    out_strict = run_ask(
        archetype_id=None,
        query="file type inventory",
        no_color=True,
        use_reranker=False,
        quiet=True,
        strict=True,
        silo="stuff-deadbeef",
    )
    assert out_loose == out_strict == ".md 2\n.pdf 1"


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


def test_run_ask_code_language_year_scoped_bypasses_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_CODE_LANGUAGE)
    monkeypatch.setattr(
        "query.core.get_code_language_stats_from_manifest_year",
        lambda db_path, silo, year: ({".py": 2}, {".py": ["/tmp/a.py"]}),
    )
    monkeypatch.setattr(
        "query.core.format_code_language_year_answer",
        lambda year, by_ext, sample_paths, source_label, no_color: f"In {year}: {next(iter(by_ext.keys()))}",
    )

    out = run_ask(archetype_id=None, query="which language did i code in 2022?", no_color=True, use_reranker=False)
    assert "In 2022: .py" in out
    assert mock_ollama["calls"] == []


def test_run_ask_code_activity_year_lookup_applies_code_year_source_filter(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    monkeypatch.setattr(
        "query.core.get_code_sources_from_manifest_year",
        lambda db_path, silo, year: ["/tmp/a.py", "/tmp/b.py"],
    )
    mock_collection.query_result = {
        "documents": [["worked on parser refactor", "built api utility"]],
        "metadatas": [[
            {"source": "/tmp/a.py", "is_local": 1},
            {"source": "/tmp/b.py", "is_local": 1},
        ]],
        "distances": [[0.1, 0.2]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(archetype_id=None, query="what was i coding in 2022?", no_color=True, use_reranker=False)
    q_calls = [kwargs for name, kwargs in mock_collection.calls if name == "query"]
    assert q_calls
    assert q_calls[0]["where"] == {"source": {"$in": ["/tmp/a.py", "/tmp/b.py"]}}
    assert "working on code across 2 file(s)" in out
    assert "a.py" in out
    assert "b.py" in out
    assert len(mock_ollama["calls"]) == 0


def test_run_ask_code_activity_year_lookup_no_code_files_returns_deterministic_message(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.get_code_sources_from_manifest_year",
        lambda db_path, silo, year: [],
    )

    out = run_ask(archetype_id=None, query="what was i coding in 2022?", no_color=True, use_reranker=False)
    assert "I couldn't find code files from 2022 in llmli." in out
    assert mock_ollama["calls"] == []


def test_run_ask_code_activity_year_lookup_skips_subscope_resolution(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.resolve_subscope",
        lambda _q, _db, _gps: (_ for _ in ()).throw(AssertionError("subscope should be skipped for code-activity-year lookup")),
    )
    monkeypatch.setattr(
        "query.core.get_code_sources_from_manifest_year",
        lambda db_path, silo, year: ["/tmp/a.py"],
    )
    mock_collection.query_result = {
        "documents": [["did coding work"]],
        "metadatas": [[{"source": "/tmp/a.py", "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    out = run_ask(archetype_id=None, query="what was i coding in 2022?", no_color=True, use_reranker=False)
    assert "working on code across 1 file(s)" in out
    assert len(mock_ollama["calls"]) == 0


def test_run_ask_code_activity_year_lookup_suppresses_low_conf_and_summarizes_themes(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    monkeypatch.setattr(
        "query.core.get_code_sources_from_manifest_year",
        lambda db_path, silo, year: ["/tmp/a.py", "/tmp/b.py"],
    )
    mock_collection.query_result = {
        "documents": [["simple python script", "tkinter helper"]],
        "metadatas": [[
            {"source": "/tmp/a.py", "is_local": 1},
            {"source": "/tmp/b.py", "is_local": 1},
        ]],
        "distances": [[0.95, 0.91]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(archetype_id=None, query="what was i coding in 2022?", no_color=True, use_reranker=False)
    assert "Low confidence: query is weakly related to indexed content." not in out
    assert "GUI apps (tkinter/PIL)" in out
    assert len(mock_ollama["calls"]) == 0


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


def test_run_ask_unified_balances_final_context_across_silos(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    docs = [f"openclaw context {i}" for i in range(1, 13)] + ["tax context 1", "tax context 2"]
    metas = [
        {"source": f"/tmp/openclaw-{i}.ts", "is_local": 1, "silo": "openclaw-aa2df362"}
        for i in range(1, 13)
    ] + [
        {"source": "/tmp/tax-1.pdf", "is_local": 1, "silo": "tax"},
        {"source": "/tmp/tax-2.pdf", "is_local": 1, "silo": "tax"},
    ]
    mock_collection.query_result = {
        "documents": [docs],
        "metadatas": [metas],
        "distances": [[0.1] * len(docs)],
        "ids": [[f"id-{i}" for i in range(1, len(docs) + 1)]],
    }
    run_ask(
        archetype_id=None,
        query="what recurring themes are across my silos",
        no_color=True,
        use_reranker=False,
        n_results=20,
        explicit_unified=True,
    )
    assert len(mock_ollama["calls"]) == 1
    context = mock_ollama["calls"][0]["messages"][1]["content"]
    assert "[silo: tax]" in context
    assert context.count("[silo: openclaw-aa2df362]") <= 3


def test_run_ask_unified_analytical_fanout_groups_context_by_silo(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    calls = []

    def _query(**kwargs):
        calls.append(kwargs)
        where = kwargs.get("where")
        where_repr = repr(where)
        if where is None:
            return {
                "documents": [[
                    "openclaw primary",
                    "openclaw transport",
                    "tax summary seed",
                    "self repo seed",
                ]],
                "metadatas": [[
                    {"source": "/tmp/openclaw/a.ts", "is_local": 1, "silo": "openclaw-aa2df362"},
                    {"source": "/tmp/openclaw/b.ts", "is_local": 1, "silo": "openclaw-aa2df362"},
                    {"source": "/tmp/tax/return.pdf", "is_local": 1, "silo": "tax-123"},
                    {"source": "/tmp/self/core.py", "is_local": 1, "silo": "__self__"},
                ]],
                "distances": [[0.20, 0.22, 0.25, 0.27]],
                "ids": [["id-1", "id-2", "id-3", "id-4"]],
            }
        if "openclaw-aa2df362" in where_repr:
            return {
                "documents": [["openclaw fanout"]],
                "metadatas": [[{"source": "/tmp/openclaw/c.ts", "is_local": 1, "silo": "openclaw-aa2df362"}]],
                "distances": [[0.18]],
                "ids": [["id-5"]],
            }
        if "tax-123" in where_repr:
            return {
                "documents": [["tax fanout"]],
                "metadatas": [[{"source": "/tmp/tax/w2.pdf", "is_local": 1, "silo": "tax-123"}]],
                "distances": [[0.19]],
                "ids": [["id-6"]],
            }
        if "__self__" in where_repr:
            return {
                "documents": [["self fanout"]],
                "metadatas": [[{"source": "/tmp/self/tests.py", "is_local": 1, "silo": "__self__"}]],
                "distances": [[0.21]],
                "ids": [["id-7"]],
            }
        return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

    mock_collection.query = _query  # type: ignore[method-assign]
    run_ask(
        archetype_id=None,
        query="what recurring themes are across silos versus traditional workflows?",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
    )
    assert len(calls) >= 4  # stage A + per-silo fanout
    context = mock_ollama["calls"][0]["messages"][1]["content"]
    assert "[SILO GROUP: openclaw-aa2df362]" in context
    assert "[SILO GROUP: tax-123]" in context
    assert "[SILO GROUP: __self__]" in context


def test_run_ask_unified_analytical_adds_single_silo_concentration_note(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    mock_collection.query_result = {
        "documents": [["openclaw only evidence", "more openclaw evidence"]],
        "metadatas": [[
            {"source": "/tmp/openclaw/a.ts", "is_local": 1, "silo": "openclaw-aa2df362"},
            {"source": "/tmp/openclaw/b.ts", "is_local": 1, "silo": "openclaw-aa2df362"},
        ]],
        "distances": [[0.20, 0.21]],
        "ids": [["id-1", "id-2"]],
    }
    mock_ollama["response"] = {"message": {"content": "Primary findings listed."}}
    out = run_ask(
        archetype_id=None,
        query="what recurring themes are across silos?",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
    )
    assert "Evidence concentration note: retrieved evidence was concentrated in one silo" in out


def test_run_ask_unified_analytical_soft_promotion_adds_close_alt_silo(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    monkeypatch.setattr("query.core._rank_candidate_silos", lambda _m, _d, max_candidates=6: [])
    mock_collection.query_result = {
        "documents": [[
            "alpha one",
            "alpha two",
            "alpha three",
            "alpha four",
            "beta one",
            "gamma one",
        ]],
        "metadatas": [[
            {"source": "/tmp/a1.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a2.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a3.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a4.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/b1.md", "is_local": 1, "silo": "beta"},
            {"source": "/tmp/c1.md", "is_local": 1, "silo": "gamma"},
        ]],
        "distances": [[0.10, 0.11, 0.12, 0.13, 0.16, 0.17]],
        "ids": [["id-1", "id-2", "id-3", "id-4", "id-5", "id-6"]],
    }
    run_ask(
        archetype_id=None,
        query="what recurring themes are across silos?",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
        n_results=4,
    )
    context = mock_ollama["calls"][0]["messages"][1]["content"]
    assert "[SILO GROUP: alpha]" in context
    assert ("[SILO GROUP: beta]" in context) or ("[SILO GROUP: gamma]" in context)


def test_run_ask_unified_analytical_soft_promotion_skips_weak_alt_silo(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)
    monkeypatch.setattr("query.core._rank_candidate_silos", lambda _m, _d, max_candidates=6: [])
    mock_collection.query_result = {
        "documents": [["alpha one", "alpha two", "alpha three", "alpha four", "beta far"]],
        "metadatas": [[
            {"source": "/tmp/a1.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a2.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a3.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/a4.md", "is_local": 1, "silo": "alpha"},
            {"source": "/tmp/b1.md", "is_local": 1, "silo": "beta"},
        ]],
        "distances": [[0.10, 0.11, 0.12, 0.13, 0.45]],
        "ids": [["id-1", "id-2", "id-3", "id-4", "id-5"]],
    }
    mock_ollama["response"] = {"message": {"content": "Direct synthesis."}}
    out = run_ask(
        archetype_id=None,
        query="what recurring themes are across silos?",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
        n_results=4,
    )
    context = mock_ollama["calls"][0]["messages"][1]["content"]
    assert "[SILO GROUP: alpha]" in context
    assert "[SILO GROUP: beta]" not in context
    assert "Evidence concentration note: retrieved evidence was concentrated in one silo (alpha)." in out


def test_run_ask_explicit_single_silo_does_not_apply_silo_diversification(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.diversify_by_silo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not diversify by silo for explicit scope")),
    )
    mock_collection.query_result = {
        "documents": [["x"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1, "silo": "tax-1234"}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    out = run_ask(
        archetype_id=None,
        query="what",
        no_color=True,
        use_reranker=False,
        silo="tax-1234",
    )
    assert "Sources:" in out
    assert len(mock_ollama["calls"]) == 1


def test_run_ask_auto_scope_binding_applies_silo_filter(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": True}},
    )
    monkeypatch.setattr(
        "query.core.bind_scope_from_query",
        lambda _q, _db: {
            "bound_slug": "stuff-deadbeef",
            "bound_display_name": "Stuff",
            "confidence": 0.9,
            "reason": "normalized_exact",
            "cleaned_query": "main idea",
        },
    )
    mock_collection.query_result = {
        "documents": [["x"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.1]],
        "ids": [["id-1"]],
    }
    run_ask(
        archetype_id=None,
        query="main idea in my stuff",
        no_color=True,
        use_reranker=False,
    )
    q_calls = [kwargs for name, kwargs in mock_collection.calls if name == "query"]
    assert q_calls
    assert q_calls[0]["where"] == {"silo": "stuff-deadbeef"}


def test_run_ask_weak_scope_retries_single_catalog_silo(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": True}},
    )
    monkeypatch.setattr(
        "query.core.bind_scope_from_query",
        lambda _q, _db: {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "no_scope_phrase",
            "cleaned_query": _q,
        },
    )
    monkeypatch.setattr(
        "query.core.rank_silos_by_catalog_tokens",
        lambda _q, _db, _h: [{"slug": "stuff-deadbeef", "score": 5.0, "matched_tokens": ["stuff"]}],
    )

    calls = []

    def _query(**kwargs):
        calls.append(kwargs)
        where = kwargs.get("where")
        if where == {"silo": "stuff-deadbeef"}:
            return {
                "documents": [["apt context"]],
                "metadatas": [[{"source": "/tmp/APT Simulation and Analysis.pptx", "is_local": 1}]],
                "distances": [[0.2]],
                "ids": [["id-2"]],
            }
        return {
            "documents": [["weak context"]],
            "metadatas": [[{"source": "/tmp/Domain Administration.docx", "is_local": 1}]],
            "distances": [[0.92]],
            "ids": [["id-1"]],
        }

    mock_collection.query = _query  # type: ignore[method-assign]
    run_ask(
        archetype_id=None,
        query="main idea of the apt simulation powerpoint in my stuff",
        no_color=True,
        use_reranker=False,
    )
    assert len(calls) == 2
    assert calls[1]["where"] == {"silo": "stuff-deadbeef"}


def test_run_ask_explicit_unified_does_not_retry_single_silo(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(
        "query.core.load_config",
        lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": True}},
    )
    monkeypatch.setattr(
        "query.core.bind_scope_from_query",
        lambda _q, _db: {
            "bound_slug": None,
            "bound_display_name": None,
            "confidence": 0.0,
            "reason": "no_scope_phrase",
            "cleaned_query": _q,
        },
    )
    monkeypatch.setattr(
        "query.core.rank_silos_by_catalog_tokens",
        lambda _q, _db, _h: [{"slug": "tax-12345678", "score": 9.0, "matched_tokens": ["tax"]}],
    )

    calls = []

    def _query(**kwargs):
        calls.append(kwargs)
        return {
            "documents": [["weak context"]],
            "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
            "distances": [[0.92]],
            "ids": [["id-1"]],
        }

    mock_collection.query = _query  # type: ignore[method-assign]
    run_ask(
        archetype_id=None,
        query="create a timeline across school and tax",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
    )
    assert len(calls) == 1
    where_payload = calls[0].get("where")
    assert "tax-12345678" not in repr(where_payload)


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


def test_run_ask_relevance_gate_allows_scoped_lookup_with_lexical_overlap(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["Summary includes python sql linux and networking skills."]],
        "metadatas": [[{"source": "/tmp/TD-resume.docx", "is_local": 1, "silo": "job-related-stuff"}]],
        "distances": [[0.95]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {"message": {"content": "skills found"}}

    out = run_ask(
        archetype_id=None,
        query="list skills from my resume",
        no_color=True,
        use_reranker=False,
        silo="job-related-stuff",
    )
    assert "Skills found" in out
    assert len(mock_ollama["calls"]) == 1


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
    assert out == "Final answer"
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


def test_run_ask_uses_direct_address_tone_policy(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    run_ask(archetype_id=None, query="what is this", no_color=True, use_reranker=False, silo="stuff")
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert "Use a neutral, direct tone." in system_prompt
    assert "Address the user directly as 'you'/'your'." in system_prompt
    assert "Do not refer to the user in third person" in system_prompt
    assert "Do not start responses with 'You'." not in system_prompt


def test_run_ask_adds_recency_hints_for_abandoned_timeline_queries(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    mock_collection.query_result = {
        "documents": [["project paused", "follow-up notes"]],
        "metadatas": [[
            {"source": "/tmp/openclaw/a.md", "is_local": 1, "silo": "openclaw-aa2df362", "mtime": 1704067200},
            {"source": "/tmp/self/b.md", "is_local": 1, "silo": "__self__", "mtime": 1735689600},
        ]],
        "distances": [[0.2, 0.3]],
        "ids": [["id-1", "id-2"]],
    }
    run_ask(
        archetype_id=None,
        query="which projects look abandoned and what likely next step was pending?",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
    )
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    user_prompt = mock_ollama["calls"][0]["messages"][1]["content"]
    assert "Recency guidance: treat mtime recency hints as weak evidence only." in system_prompt
    assert "[RECENCY HINTS - WEAK EVIDENCE]" in user_prompt
    assert "2024-01-01" in user_prompt


def test_run_ask_adds_ownership_framing_policy_when_requested(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "is_local": 1, "silo": "s1"}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    run_ask(
        archetype_id=None,
        query="distinguish my authored work vs reference/course/vendor material",
        no_color=True,
        use_reranker=False,
    )
    system_prompt = mock_ollama["calls"][0]["messages"][0]["content"]
    assert "Ownership framing policy:" in system_prompt
    assert "syllabus/homework/transcript" in system_prompt


def test_run_ask_sanitizes_internal_context_header_artifacts(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["resume context"]],
        "metadatas": [[{"source": "/tmp/TD-resume.docx", "line_start": 24, "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {
        "message": {
            "content": (
                'Use "file=TD-resume.docx (line 24) '
                'mtime=2025-08-12 silo=job-related-stuff doc_type=other" for evidence.'
            )
        }
    }
    out = run_ask(archetype_id=None, query="what skills", no_color=True, use_reranker=False, silo="job-related-stuff")
    assert "file=" not in out
    assert "mtime=" not in out
    assert "silo=" not in out
    assert "doc_type=" not in out
    assert "TD-resume.docx (line 24)" in out


def test_run_ask_normalizes_third_person_user_phrasing(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["medical context"]],
        "metadatas": [[{"source": "/tmp/labResults.pdf", "line_start": 12, "is_local": 1}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {
        "message": {"content": "The patient should retest in 3 months. The user requested follow-up."}
    }
    out = run_ask(archetype_id=None, query="bloodwork", no_color=True, use_reranker=False, silo="documents")
    assert "The patient" not in out
    assert "The user" not in out
    assert "You should retest in 3 months." in out
    assert "You requested follow-up." in out


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


def test_run_ask_low_confidence_warning_suppressed_when_top_hit_overlaps_query(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["Prometheus metrics are part of monitoring setup.", "unrelated noisy chunk"]],
        "metadatas": [[
            {"source": "/tmp/safe_resume.docx", "is_local": 1},
            {"source": "/tmp/other.txt", "is_local": 1},
        ]],
        "distances": [[0.45, 1.20]],
        "ids": [["id-1", "id-2"]],
    }
    mock_ollama["response"] = {"message": {"content": "Prometheus is used for monitoring metrics."}}

    out = run_ask(
        archetype_id=None,
        query="what is prometheus?",
        no_color=True,
        use_reranker=False,
        silo="job-related-stuff",
    )
    assert "Low confidence: query is weakly related to indexed content." not in out
    assert "Prometheus is used for monitoring metrics." in out


def test_run_ask_uncertainty_answer_emits_low_confidence_banner_scoped_and_unscoped(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [[
            "Final report summary and visit notes.",
            "General chemistry panel history details.",
            "Project verification notes from another source.",
            "Journal entry unrelated to labs.",
        ]],
        "metadatas": [[
            {"source": "/tmp/labResults.pdf", "is_local": 1},
            {"source": "/tmp/lab-history.pdf", "is_local": 1},
            {"source": "/tmp/DOD_VERIFICATION.md", "is_local": 1},
            {"source": "/tmp/journal.md", "is_local": 1},
        ]],
        "distances": [[0.48, 0.61, 0.65, 0.68]],
        "ids": [["id-1", "id-2", "id-3", "id-4"]],
    }
    mock_ollama["response"] = {
        "message": {"content": "There is no mention of bloodwork results in the provided context."}
    }

    out_unscoped = run_ask(
        archetype_id=None,
        query="tell me about my bloodwork results",
        no_color=True,
        use_reranker=False,
        silo=None,
    )
    out_scoped = run_ask(
        archetype_id=None,
        query="tell me about my bloodwork results",
        no_color=True,
        use_reranker=False,
        silo="documents-4d300c97",
    )

    assert "Low confidence: query is weakly related to indexed content." in out_unscoped
    assert "Low confidence: query is weakly related to indexed content." in out_scoped


def test_run_ask_banner_mode_normalizes_repetitive_uncertainty_phrasing(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["weak evidence one", "weak evidence two"]],
        "metadatas": [[
            {"source": "/tmp/a.md", "is_local": 1, "silo": "s1"},
            {"source": "/tmp/b.md", "is_local": 1, "silo": "s2"},
        ]],
        "distances": [[0.92, 0.95]],
        "ids": [["id-1", "id-2"]],
    }
    mock_ollama["response"] = {
        "message": {
            "content": (
                "Based on the provided context, it appears that this is uncertain. "
                "It appears that there are mixed signals. "
                "Without more information, it is difficult to be definitive."
            )
        }
    }
    out = run_ask(archetype_id=None, query="what happened?", no_color=True, use_reranker=False)
    assert "Low confidence: query is weakly related to indexed content." in out
    assert "Based on the provided context" not in out
    assert out.lower().count("it appears that") <= 1
    assert "Caveat:" in out


def test_run_ask_strict_mode_keeps_original_uncertainty_phrasing(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["weak evidence one", "weak evidence two"]],
        "metadatas": [[
            {"source": "/tmp/a.md", "is_local": 1, "silo": "s1"},
            {"source": "/tmp/b.md", "is_local": 1, "silo": "s2"},
        ]],
        "distances": [[0.92, 0.95]],
        "ids": [["id-1", "id-2"]],
    }
    mock_ollama["response"] = {
        "message": {
            "content": "Based on the provided context, it appears that evidence is limited."
        }
    }
    out = run_ask(archetype_id=None, query="what happened?", no_color=True, use_reranker=False, strict=True)
    assert "Based on the provided context, it appears that evidence is limited." in out


def test_run_ask_normalizes_inline_numbered_lists_and_sentence_start(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["context"]],
        "metadatas": [[{"source": "/tmp/a.md", "is_local": 1, "silo": "s1"}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {"message": {"content": "here are findings: 1. alpha 2. beta"}}
    out = run_ask(archetype_id=None, query="what happened?", no_color=True, use_reranker=False)
    assert "Here are findings:" in out
    assert "\n1. alpha" in out
    assert "\n2. beta" in out


def test_run_ask_normalizes_ownership_claim_conflicts(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["context"]],
        "metadatas": [[{"source": "/tmp/a.md", "is_local": 1, "silo": "s1"}]],
        "distances": [[0.2]],
        "ids": [["id-1"]],
    }
    mock_ollama["response"] = {
        "message": {
            "content": "This appears authored by you, suggesting they were written by someone else."
        }
    }
    out = run_ask(archetype_id=None, query="ownership split", no_color=True, use_reranker=False)
    assert "written by someone else" not in out.lower()
    assert "ownership is uncertain" in out.lower()


def test_run_ask_confident_direct_answer_does_not_emit_low_confidence(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [[
            "Prometheus is used for collecting monitoring metrics.",
            "Prometheus integrates with Grafana.",
            "Monitoring stack includes alerting.",
        ]],
        "metadatas": [[
            {"source": "/tmp/safe_resume.docx", "is_local": 1},
            {"source": "/tmp/README.md", "is_local": 1},
            {"source": "/tmp/docs.md", "is_local": 1},
        ]],
        "distances": [[0.11, 0.18, 0.27]],
        "ids": [["id-1", "id-2", "id-3"]],
    }
    mock_ollama["response"] = {"message": {"content": "Prometheus is a monitoring system."}}

    out = run_ask(
        archetype_id=None,
        query="what is prometheus?",
        no_color=True,
        use_reranker=False,
        silo="job-related-stuff",
    )
    assert "Low confidence: query is weakly related to indexed content." not in out
    assert "Prometheus is a monitoring system." in out


def test_run_ask_trace_includes_confidence_diagnostics(monkeypatch, tmp_path, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("LLMLIBRARIAN_TRACE", str(trace_path))
    mock_collection.query_result = {
        "documents": [[
            "Final report summary and visit notes.",
            "General chemistry panel history details.",
            "Project verification notes from another source.",
        ]],
        "metadatas": [[
            {"source": "/tmp/labResults.pdf", "is_local": 1},
            {"source": "/tmp/lab-history.pdf", "is_local": 1},
            {"source": "/tmp/DOD_VERIFICATION.md", "is_local": 1},
        ]],
        "distances": [[0.48, 0.61, 0.65]],
        "ids": [["id-1", "id-2", "id-3"]],
    }
    mock_ollama["response"] = {
        "message": {"content": "There is no mention of bloodwork results in the provided context."}
    }

    run_ask(
        archetype_id=None,
        query="tell me about my bloodwork results",
        no_color=True,
        use_reranker=False,
        silo="documents-4d300c97",
    )
    payload = json.loads(trace_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "confidence_top_distance" in payload
    assert "confidence_avg_distance" in payload
    assert "confidence_source_count" in payload
    assert "confidence_overlap_support" in payload
    assert "confidence_reason" in payload
    assert "confidence_banner_emitted" in payload


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


def test_run_ask_income_employer_guardrail_short_circuits_llm(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_MONEY_YEAR_TOTAL)
    mock_collection.get_result = {
        "ids": ["id-1"],
        "documents": [
            "Form W-2 Wage and Tax Statement\nEmployer: YMCA\nBox 1 of W-2: 4,626.76",
        ],
        "metadatas": [
            {"source": "/Users/x/Tax/2025/ymca-w2-2025.pdf", "silo": "tax"},
        ],
    }

    out = run_ask(
        archetype_id=None,
        query="how much did i make in 2025 at ymca",
        no_color=True,
        use_reranker=False,
        silo="tax",
    )
    assert "W-2 wages for YMCA (2025): 4,626.76 [Box 1]" in out
    assert mock_ollama["calls"] == []
    assert not any(name == "query" for name, _kwargs in mock_collection.calls)


def test_run_ask_tax_resolver_short_circuits_llm(monkeypatch, mock_ollama):
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_TAX_QUERY)
    monkeypatch.setattr(
        "query.core.run_tax_resolver",
        lambda **_kwargs: {
            "response": "Federal income tax withheld at Deloitte (2025): 4,723.31",
            "guardrail_no_match": False,
            "guardrail_reason": "tax_resolved",
            "requested_year": "2025",
            "requested_form": "W2",
            "requested_line": "w2_box_2_federal_income_tax_withheld",
            "num_docs": 1,
            "receipt_metas": [{"source": "/tmp/deloitte.pdf", "page": 1}],
        },
    )
    monkeypatch.setattr(
        "query.core.chromadb.PersistentClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("tax resolver should bypass chroma retrieval")),
    )
    out = run_ask(
        archetype_id=None,
        query="box 2 deloitte 2025",
        no_color=True,
        use_reranker=False,
        silo="tax",
    )
    assert "4,723.31" in out
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


def test_run_ask_presentation_low_confidence_message_is_specific(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["slide content a", "slide content b"]],
        "metadatas": [[
            {"source": "/tmp/A.pptx", "line_start": 1, "is_local": 1},
            {"source": "/tmp/B.pptx", "line_start": 2, "is_local": 1},
        ]],
        "distances": [[0.95, 0.94]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(
        archetype_id=None,
        query="main idea of this powerpoint",
        no_color=True,
        use_reranker=False,
    )
    assert "matched multiple presentations" in out


def test_run_ask_unified_synthesis_low_confidence_message_is_specific(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["tax context", "school context"]],
        "metadatas": [[
            {"source": "/tmp/tax.pdf", "line_start": 1, "is_local": 1},
            {"source": "/tmp/school.docx", "line_start": 2, "is_local": 1},
        ]],
        "distances": [[0.95, 0.91]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(
        archetype_id=None,
        query="create a timeline of major events across school tax and work",
        no_color=True,
        use_reranker=False,
        explicit_unified=True,
    )
    assert "unified search found weak or uneven evidence across silos" in out


def test_run_ask_footer_dedupes_duplicate_source_with_aggregated_lines(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["chunk one", "chunk two"]],
        "metadatas": [[
            {"source": "/tmp/APT Simulation and Analysis.pptx", "line_start": 1, "is_local": 1},
            {"source": "/tmp/APT Simulation and Analysis.pptx", "line_start": 23, "is_local": 1},
        ]],
        "distances": [[0.2, 0.3]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(archetype_id=None, query="main idea", no_color=True, use_reranker=False)
    assert out.count("APT Simulation and Analysis.pptx (line") == 1
    assert "(line 1, 23)" in out


def test_run_ask_filetype_floor_filters_unrelated_presentations(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    mock_collection.query_result = {
        "documents": [["apt good", "loops noise"]],
        "metadatas": [[
            {"source": "/tmp/APT Simulation and Analysis.pptx", "line_start": 1, "is_local": 1},
            {"source": "/tmp/Chapter 4 Loops.pptx", "line_start": 1, "is_local": 1},
        ]],
        "distances": [[0.25, 0.95]],
        "ids": [["id-1", "id-2"]],
    }
    out = run_ask(
        archetype_id=None,
        query="main idea of the apt simulation powerpoint",
        no_color=True,
        use_reranker=False,
    )
    assert "APT Simulation and Analysis.pptx" in out
    assert "Chapter 4 Loops.pptx" not in out
