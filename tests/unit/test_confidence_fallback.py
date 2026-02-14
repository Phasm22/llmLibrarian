from query.core import run_ask
from query.intent import INTENT_LOOKUP


class _DummyClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _patch_query_runtime(monkeypatch, mock_collection):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))


def test_confidence_fallback_returns_structure_outline_for_scoped_lookup(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: "Stuff")
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {}})
    monkeypatch.setattr(
        "query.core.build_structure_outline",
        lambda _db, _silo, cap=50: {
            "mode": "outline",
            "lines": ["docs/a.md", "notes/b.md", "slides/c.pptx"],
            "scanned_count": 9,
            "matched_count": 3,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": False,
            "stale_reason": None,
        },
    )
    mock_collection.query_result = {
        "documents": [["weak context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "stuff-deadbeef"}]],
        "distances": [[9.9]],
        "ids": [["id-1"]],
    }

    out = run_ask(
        archetype_id=None,
        query="nonsense lookup question",
        no_color=True,
        use_reranker=False,
        silo="stuff-deadbeef",
    )

    assert "I don't have content closely matching that query." in out
    assert "Here's what I have indexed in Stuff (3 files):" in out
    assert "docs/a.md" in out
    assert "notes/b.md" in out
    assert "Answered by: Stuff (structure fallback)" in out
    assert mock_ollama["calls"] == []


def test_confidence_fallback_quiet_mode_omits_footer(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: "Stuff")
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {}})
    monkeypatch.setattr(
        "query.core.build_structure_outline",
        lambda _db, _silo, cap=50: {
            "mode": "outline",
            "lines": ["docs/a.md"],
            "scanned_count": 9,
            "matched_count": 1,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": False,
            "stale_reason": None,
        },
    )
    mock_collection.query_result = {
        "documents": [["weak context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "stuff-deadbeef"}]],
        "distances": [[9.9]],
        "ids": [["id-1"]],
    }

    out = run_ask(
        archetype_id=None,
        query="nonsense lookup question",
        no_color=True,
        use_reranker=False,
        quiet=True,
        silo="stuff-deadbeef",
    )

    assert "I don't have content closely matching that query." in out
    assert "docs/a.md" in out
    assert "Answered by:" not in out
    assert "---" not in out
    assert mock_ollama["calls"] == []


def test_confidence_fallback_uses_generic_message_when_structure_unavailable(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.get_silo_display_name", lambda _db, _silo: "Stuff")
    monkeypatch.setattr("query.core.get_silo_prompt_override", lambda _db, _silo: None)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {}})
    monkeypatch.setattr(
        "query.core.build_structure_outline",
        lambda _db, _silo, cap=50: {
            "mode": "outline",
            "lines": [],
            "scanned_count": 9,
            "matched_count": 0,
            "cap_applied": False,
            "scope": "silo:stuff-deadbeef",
            "stale": True,
            "stale_reason": "manifest_missing",
        },
    )
    mock_collection.query_result = {
        "documents": [["weak context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1, "silo": "stuff-deadbeef"}]],
        "distances": [[9.9]],
        "ids": [["id-1"]],
    }

    out = run_ask(
        archetype_id=None,
        query="nonsense lookup question",
        no_color=True,
        use_reranker=False,
        quiet=False,
        silo="stuff-deadbeef",
    )

    assert "I don't have relevant content for that." in out
    assert "structure fallback" not in out
    assert "Answered by: Stuff" in out
    assert mock_ollama["calls"] == []


def test_confidence_fallback_without_scope_stays_generic(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {}})
    mock_collection.query_result = {
        "documents": [["weak context"]],
        "metadatas": [[{"source": "/tmp/a.txt", "line_start": 3, "is_local": 1}]],
        "distances": [[9.9]],
        "ids": [["id-1"]],
    }

    out = run_ask(
        archetype_id=None,
        query="nonsense lookup question",
        no_color=True,
        use_reranker=False,
        quiet=False,
        silo=None,
    )

    assert "I don't have relevant content for that." in out
    assert "Here's what I have indexed" not in out
    assert mock_ollama["calls"] == []
