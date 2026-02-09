from pathlib import Path

from query.intent import INTENT_PROJECT_COUNT
from query.core import run_ask


class _DummyClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _patch_query_runtime(monkeypatch, mock_collection, tmp_path: Path):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))
    monkeypatch.setattr("query.core._get_silo_root", lambda _db, _silo: str(tmp_path / "root"))
    monkeypatch.setattr(
        "query.core.get_paths_by_silo",
        lambda _db: {
            "silo-x": {
                str(tmp_path / "root" / "a" / "f1.py"),
                str(tmp_path / "root" / "b" / "f2.js"),
                str(tmp_path / "root" / "c.md"),
            }
        },
    )


def test_project_count_from_registry(monkeypatch, mock_collection, mock_ollama, tmp_path):
    _patch_query_runtime(monkeypatch, mock_collection, tmp_path)
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_PROJECT_COUNT)
    out = run_ask(archetype_id=None, query="how many coding projects are here", no_color=True, use_reranker=False, silo="silo-x", db_path=tmp_path / "db")
    assert "Found 2 coding project folders" in out
    assert "a/f1.py" in out or "b/f2.js" in out
    assert mock_ollama["calls"] == []


def test_project_count_falls_back_to_collection_when_no_registry(monkeypatch, mock_collection, mock_ollama, tmp_path):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))
    monkeypatch.setattr("query.core.get_paths_by_silo", None)
    monkeypatch.setattr("query.core._get_silo_root", lambda _db, _silo: str(tmp_path / "root"))
    mock_collection.get_result = {
        "metadatas": [
            {"source": str(tmp_path / "root" / "proj1" / "main.py")},
            {"source": str(tmp_path / "root" / "proj2" / "app.ts")},
            {"source": str(tmp_path / "root" / "notes.txt")},
        ]
    }
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_PROJECT_COUNT)
    out = run_ask(archetype_id=None, query="how many coding projects are here", no_color=True, use_reranker=False, silo="silo-y", db_path=tmp_path / "db")
    assert "Found 2 coding project folders" in out
    assert mock_ollama["calls"] == []


def test_project_count_no_code_files(monkeypatch, mock_collection, mock_ollama, tmp_path):
    monkeypatch.setattr("query.core.get_embedding_function", lambda: None)
    monkeypatch.setattr("query.core.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))
    monkeypatch.setattr(
        "query.core.get_paths_by_silo",
        lambda _db: {"silo-z": {str(tmp_path / "root" / "a" / "note.md")}},
    )
    monkeypatch.setattr("query.core._get_silo_root", lambda _db, _silo: str(tmp_path / "root"))
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_PROJECT_COUNT)
    out = run_ask(archetype_id=None, query="how many coding projects are here", no_color=True, use_reranker=False, silo="silo-z", db_path=tmp_path / "db")
    assert "No code files indexed for" in out
    assert mock_ollama["calls"] == []
