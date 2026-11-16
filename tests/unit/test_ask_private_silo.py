"""Unscoped pal/llmli ask must not retrieve private silos."""
import json

from query.intent import INTENT_LOOKUP
from query.core import run_ask


class _Collection:
    def __init__(self):
        self.wheres: list = []

    def query(self, **kwargs):
        self.wheres.append(kwargs.get("where"))
        return {
            "documents": [["a public note"]],
            "metadatas": [[{"source": "/notes/a.md", "silo": "notes", "chunk_hash": "abc"}]],
            "distances": [[0.2]],
            "ids": [["1"]],
        }

    def get(self, **_kwargs):
        return {"ids": [], "documents": [], "metadatas": []}


class _Client:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _registry(db):
    db.mkdir()
    (db / "llmli_registry.json").write_text(
        json.dumps(
            {
                "notes": {"slug": "notes", "display_name": "Notes", "path": "/notes", "private": False},
                "tax-1": {"slug": "tax-1", "display_name": "Tax", "path": "/tax", "private": True},
            }
        )
    )


def _patch(monkeypatch, collection):
    monkeypatch.setattr("query.core.get_embedding_function", lambda **_kw: None)
    monkeypatch.setattr("query.core.get_client", lambda db_path: _Client(collection))
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.load_config", lambda *a, **k: {})
    monkeypatch.setattr("query.core.resolve_subscope", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "query.core.bind_scope_from_query",
        lambda *_a, **_k: {
            "reason": None,
            "confidence": 0.0,
            "cleaned_query": None,
            "bound_slug": None,
            "bound_display_name": None,
        },
    )
    monkeypatch.setattr("query.core.is_reranker_enabled", lambda: False)
    monkeypatch.setattr("query.core.get_query_options", lambda *_a, **_k: {"auto_scope_binding": False})
    monkeypatch.setattr("query.core.get_image_embedding_adapter", lambda: None)


def _nin_slugs(where) -> list[str]:
    if not isinstance(where, dict):
        return []
    silo = where.get("silo")
    if isinstance(silo, dict) and "$nin" in silo:
        return list(silo["$nin"])
    for part in where.get("$and") or []:
        found = _nin_slugs(part)
        if found:
            return found
    return []


def test_unscoped_ask_excludes_private_silos(monkeypatch, mock_ollama, tmp_path):
    db = tmp_path / "db"
    _registry(db)
    collection = _Collection()
    _patch(monkeypatch, collection)

    out = run_ask(
        archetype_id=None,
        query="what did I note about the move",
        no_color=True,
        use_reranker=False,
        quiet=True,
        db_path=db,
    )
    assert collection.wheres, "ask never queried"
    skipped = _nin_slugs(collection.wheres[0])
    assert "tax-1" in skipped
    assert "tax-1-artifacts" in skipped
    assert "tax-1" in out
    assert "private" in out
    assert mock_ollama["calls"]


def test_named_silo_ask_does_not_exclude_it(monkeypatch, mock_ollama, tmp_path):
    db = tmp_path / "db"
    _registry(db)
    collection = _Collection()
    _patch(monkeypatch, collection)

    out = run_ask(
        archetype_id=None,
        query="what is my agi",
        no_color=True,
        use_reranker=False,
        quiet=True,
        db_path=db,
        silo="tax-1",
    )
    assert collection.wheres
    where = collection.wheres[0]
    assert where == {"silo": "tax-1"}
    assert "were not searched" not in out
