from query.intent import INTENT_MONEY_YEAR_TOTAL
from query.core import run_ask


class _DummyClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _patch_query_runtime(monkeypatch, mock_collection):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))


def test_income_plan_returns_line9_when_present(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_MONEY_YEAR_TOTAL)
    mock_collection.get_result = {
        "ids": ["id-1", "id-2"],
        "documents": [
            "Form 1040 (2024)\nline 9: 7,522.",
            "Form 1040 (2021)\nline 9: 99,999.",
        ],
        "metadatas": [
            {"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf", "silo": "tax"},
            {"source": "/Users/x/Tax/2021/2021_TaxReturn.pdf", "silo": "tax"},
        ],
    }
    out = run_ask(archetype_id=None, query="what was my income in 2024", no_color=True, use_reranker=False, silo="tax")
    assert "Form 1040 line 9 (2024): 7,522." in out
    assert mock_ollama["calls"] == []


def test_income_plan_falls_back_to_line11(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_MONEY_YEAR_TOTAL)
    mock_collection.get_result = {
        "ids": ["id-2"],
        "documents": ["Form 1040 (2024)\nline 11: 14,600."],
        "metadatas": [{"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf", "silo": "tax"}],
    }
    out = run_ask(archetype_id=None, query="what was my income in 2024", no_color=True, use_reranker=False, silo="tax")
    assert "fallback from line 9" in out
    assert "line 11" in out
    assert mock_ollama["calls"] == []


def test_income_plan_disambiguates_when_no_line_found(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_MONEY_YEAR_TOTAL)
    mock_collection.get_result = {
        "ids": ["id-3"],
        "documents": ["Form 1040 (2024)\nsummary only"],
        "metadatas": [{"source": "/Users/x/Tax/2024/2024 Federal Income Tax Return.pdf", "silo": "tax"}],
    }
    out = run_ask(archetype_id=None, query="what was my income in 2024", no_color=True, use_reranker=False, silo="tax")
    assert "I can't find a single income field for 2024." in out
    assert mock_ollama["calls"] == []


def test_income_plan_reports_missing_year(monkeypatch, mock_collection, mock_ollama):
    _patch_query_runtime(monkeypatch, mock_collection)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_MONEY_YEAR_TOTAL)
    mock_collection.get_result = {
        "ids": ["id-1"],
        "documents": ["Form 1040 (2021)\nline 9: 99,999."],
        "metadatas": [{"source": "/Users/x/Tax/2021/2021_TaxReturn.pdf", "silo": "tax"}],
    }
    out = run_ask(archetype_id=None, query="what was my income in 2024", no_color=True, use_reranker=False, silo="tax")
    assert "I could not find indexed tax documents for 2024 in this silo." in out
    assert mock_ollama["calls"] == []
