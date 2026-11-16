"""LOOKUP diversity caps: scoped and source-restricted reads keep n_results.

The per-source cap of 3 keeps one big file from dominating *unscoped* retrieval.
When the caller already narrowed the read to one silo or one file, that cap
throws away exactly the chunks they asked for: a one-file cookbook silo returned
three of eight recipe chunks, and source= returned three chunks of the file it
named. These fixtures pin the candidate set before synthesis or context budgeting.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import query.ask.orchestrator as orchestrator
from query.core import run_ask, run_retrieve
from query.intent import INTENT_LOOKUP
from state import set_silo_private, update_silo

COOKBOOK = "cookbook-c00k"
NOTES = "notes-n0te"
PRIVATE = "tax-abc"
BOOK = "/corpus/cookbook/cookbook.md"
OTHER_NOTE = "/corpus/notes/pantry.md"
PRIVATE_FILE = "/corpus/tax/return.md"
N = 8


def _rows() -> list[tuple[str, str, dict[str, Any], float]]:
    rows: list[tuple[str, str, dict[str, Any], float]] = []
    for i in range(N):
        rows.append((f"book-{i}", f"recipe {i}: carrot cake variant {i}", {"source": BOOK, "silo": COOKBOOK, "chunk_index": i, "is_local": 1}, 0.10 + i * 0.01))
    for i in range(4):
        rows.append((f"note-{i}", f"pantry note {i} carrot", {"source": OTHER_NOTE, "silo": NOTES, "chunk_index": i, "is_local": 1}, 0.12 + i * 0.01))
    for i in range(4):
        rows.append((f"priv-{i}", f"private line {i} carrot", {"source": PRIVATE_FILE, "silo": PRIVATE, "chunk_index": i, "is_local": 1}, 0.05 + i * 0.01))
    for i in range(4):
        rows.append((f"art-{i}", f"artifact summary {i} carrot", {"source": f"/artifacts/cookbook-{i}.md", "silo": f"{COOKBOOK}-artifacts", "chunk_index": 0, "is_local": 1}, 0.11 + i * 0.01))
    return rows


def _match(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    if not where:
        return True
    if "$and" in where:
        return all(_match(meta, part) for part in where["$and"])
    for key, cond in where.items():
        val = meta.get(key)
        if isinstance(cond, dict):
            if "$nin" in cond and val in cond["$nin"]:
                return False
            if "$in" in cond and val not in cond["$in"]:
                return False
        elif val != cond:
            return False
    return True


class _WhereCollection:
    """Chroma stand-in that honors the where-clause the caller built."""

    def __init__(self) -> None:
        self.rows = _rows()
        self.wheres: list[dict | None] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        where = kw.get("where")
        self.wheres.append(where)
        hits = sorted((r for r in self.rows if _match(r[2], where)), key=lambda r: r[3])[: kw.get("n_results", 10)]
        return {
            "ids": [[h[0] for h in hits]],
            "documents": [[h[1] for h in hits]],
            "metadatas": [[dict(h[2]) for h in hits]],
            "distances": [[h[3] for h in hits]],
        }

    def get(self, **_kw: Any) -> dict[str, Any]:
        return {"ids": [], "documents": [], "metadatas": []}


class _Client:
    def __init__(self, coll: _WhereCollection) -> None:
        self.coll = coll

    def get_or_create_collection(self, **_kw: Any) -> _WhereCollection:
        return self.coll


@pytest.fixture()
def db(tmp_path: Path) -> str:
    root = tmp_path / "db"
    root.mkdir()
    update_silo(str(root), COOKBOOK, "/corpus/cookbook", 1, N, "2026-10-01T00:00:00+00:00", display_name="Cookbook")
    update_silo(str(root), NOTES, "/corpus/notes", 1, 4, "2026-10-01T00:00:00+00:00", display_name="Notes")
    update_silo(str(root), PRIVATE, "/corpus/tax", 1, 4, "2026-10-01T00:00:00+00:00", display_name="Tax")
    set_silo_private(str(root), PRIVATE, True)
    manifest = {
        "silos": {
            COOKBOOK: {"path": "/corpus/cookbook", "files": {BOOK: {}}},
            NOTES: {"path": "/corpus/notes", "files": {OTHER_NOTE: {}}},
            PRIVATE: {"path": "/corpus/tax", "files": {PRIVATE_FILE: {}}},
        }
    }
    (root / "llmli_file_manifest.json").write_text(json.dumps(manifest))
    return str(root)


@pytest.fixture()
def coll(monkeypatch: pytest.MonkeyPatch) -> _WhereCollection:
    c = _WhereCollection()
    passthrough = lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only")  # noqa: E731
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr("query.core.run_hybrid_retrieve", passthrough)
    monkeypatch.setattr("query.retrieve_locked.run_hybrid_retrieve", passthrough)
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda **_kw: None)
    monkeypatch.setattr("query.retrieve_locked.get_image_embedding_adapter", lambda: None)
    monkeypatch.setattr("query.core.get_embedding_function", lambda **_kw: None)
    monkeypatch.setattr("query.core.get_client", lambda _db: _Client(c))
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda _db, _silo: False)
    return c


def _sources(chunks: list[dict]) -> list[str]:
    return [str(c.get("source") or "") for c in chunks]


def _capture_ask_candidates(monkeypatch: pytest.MonkeyPatch) -> list[list[dict | None]]:
    """Record the chunk set handed to the context budget (i.e. before it trims)."""
    seen: list[list[dict | None]] = []
    real = orchestrator._apply_context_budget

    def _spy(docs, metas, dists, **kw):
        seen.append(list(metas))
        return real(docs, metas, dists, **kw)

    monkeypatch.setenv("LLMLIBRARIAN_CONTEXT_BUDGET_TOKENS", "10000000")
    monkeypatch.setattr(orchestrator, "_apply_context_budget", _spy)
    return seen


def _no_auto_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": False}})
    monkeypatch.setattr("query.core.resolve_subscope", lambda _q, _db, _gps: None)


# --- run_retrieve (MCP query_personal_knowledge / llmli query) -----------------


def test_scoped_retrieve_returns_eight_chunks_from_one_file(db, coll):
    res = run_retrieve("carrot cake", silo=COOKBOOK, n_results=N, db_path=db)
    assert _sources(res["chunks"]) == [BOOK] * N


def test_source_only_retrieve_returns_eight_chunks_from_that_file(db, coll):
    res = run_retrieve("carrot cake", source=BOOK, n_results=N, db_path=db)
    assert _sources(res["chunks"]) == [BOOK] * N
    # No silo was named, so none is reported and no consent was fabricated.
    assert res["silo_filter"] is None


def test_source_only_retrieve_keeps_private_exclusion_in_where(db, coll):
    run_retrieve("carrot cake", source=BOOK, n_results=N, db_path=db)
    where = coll.wheres[0]
    assert where == {"$and": [{"silo": {"$nin": [PRIVATE, f"{PRIVATE}-artifacts"]}}, {"source": BOOK}]}


def test_source_only_retrieve_cannot_reach_a_private_file(db, coll):
    res = run_retrieve("carrot cake", source=PRIVATE_FILE, n_results=N, db_path=db)
    assert res["chunks"] == []


def test_unscoped_retrieve_keeps_three_per_source_and_three_per_silo(db, coll):
    res = run_retrieve("carrot cake", n_results=N, db_path=db)
    srcs = _sources(res["chunks"])
    assert srcs.count(BOOK) == 3
    assert PRIVATE_FILE not in srcs
    silos = [c["silo"] for c in res["chunks"]]
    assert max(silos.count(s) for s in set(silos)) <= 3


def test_scoped_retrieve_with_artifact_stream_keeps_raw_chunks(db, coll, monkeypatch):
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda _db, _silo: True)
    res = run_retrieve("carrot cake", silo=COOKBOOK, n_results=N, db_path=db)
    assert len(res["chunks"]) == N
    assert _sources(res["chunks"]).count(BOOK) >= 4


# --- MCP source resolution --------------------------------------------------


def test_mcp_source_only_query_returns_eight_and_passes_no_silo(db, coll, monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    seen: dict[str, Any] = {}
    real = run_retrieve

    def _spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr("query.core.run_retrieve", _spy)
    res = mcp_server.query_personal_knowledge("carrot cake", source="cookbook.md", n_results=N)
    assert seen["silo"] is None
    assert seen["source"] == BOOK
    assert _sources(res["chunks"]) == [BOOK] * N
    assert res.get("excluded_private_silos") == [PRIVATE]


def test_mcp_source_only_query_does_not_resolve_private_file(db, coll, monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    res = mcp_server.query_personal_knowledge("carrot cake", source="return.md", n_results=N)
    assert res["chunks"] == []
    assert "no indexed file" in res["error"]


# --- run_ask (pal ask / llmli ask) ------------------------------------------


def test_scoped_ask_keeps_eight_candidates_from_one_file(db, coll, monkeypatch, mock_ollama):
    seen = _capture_ask_candidates(monkeypatch)
    run_ask(None, "carrot cake recipes", silo=COOKBOOK, n_results=N, db_path=db, no_color=True, use_reranker=False)
    assert seen, "ask never reached context budgeting"
    assert [(m or {}).get("source") for m in seen[0]] == [BOOK] * N


def test_scoped_ask_with_artifact_stream_keeps_raw_chunks(db, coll, monkeypatch, mock_ollama):
    monkeypatch.setattr("state.get_silo_artifact_compile", lambda _db, _s: {"artifact_silo": f"{COOKBOOK}-artifacts"})
    seen = _capture_ask_candidates(monkeypatch)
    run_ask(None, "carrot cake recipes", silo=COOKBOOK, n_results=N, db_path=db, no_color=True, use_reranker=False)
    srcs = [(m or {}).get("source") for m in seen[0]]
    assert len(srcs) == N
    # Same bar as retrieval: the raw stream may not be held to three per file.
    assert srcs.count(BOOK) >= 4


def test_unscoped_ask_keeps_three_per_source_and_three_per_silo(db, coll, monkeypatch, mock_ollama):
    _no_auto_scope(monkeypatch)
    seen = _capture_ask_candidates(monkeypatch)
    run_ask(None, "carrot cake recipes", n_results=N, db_path=db, no_color=True, use_reranker=False)
    metas = seen[0]
    srcs = [(m or {}).get("source") for m in metas]
    silos = [(m or {}).get("silo") for m in metas]
    assert srcs.count(BOOK) == 3
    assert max(silos.count(s) for s in set(silos)) <= 3


def test_auto_bound_ask_scope_keeps_the_unscoped_source_cap(db, coll, monkeypatch, mock_ollama):
    """Automatic binding is a guess about scope, not a caller's restriction."""
    monkeypatch.setattr("query.core.load_config", lambda _p=None: {"archetypes": {}, "query": {"auto_scope_binding": True}})
    monkeypatch.setattr(
        "query.core.bind_scope_from_query",
        lambda _q, _db: {"bound_slug": COOKBOOK, "bound_display_name": "Cookbook", "confidence": 0.9, "reason": "normalized_exact", "cleaned_query": "carrot cake"},
    )
    seen = _capture_ask_candidates(monkeypatch)
    run_ask(None, "carrot cake in my cookbook", n_results=N, db_path=db, no_color=True, use_reranker=False)
    assert coll.wheres[0] == {"silo": COOKBOOK}
    assert [(m or {}).get("source") for m in seen[0]] == [BOOK] * 3


def test_scoped_ask_evidence_profile_cap_unchanged(db, coll, monkeypatch, mock_ollama):
    from query.intent import INTENT_EVIDENCE_PROFILE

    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_EVIDENCE_PROFILE)
    seen = _capture_ask_candidates(monkeypatch)
    run_ask(None, "what do I like about carrot cake", silo=COOKBOOK, n_results=N, db_path=db, no_color=True, use_reranker=False)
    assert [(m or {}).get("source") for m in seen[0]] == [BOOK] * 2


def test_unscoped_retrieve_fills_n_results_after_silo_cap(db, coll):
    """The best hits can all sit in one silo. The silo cap must still be able to fill n_results from what follows."""
    rows = []
    for i in range(6):
        rows.append(
            (f"a-{i}", f"alpha {i}", {"source": f"/a/{i}.md", "silo": "alpha", "is_local": 1}, 0.10 + i * 0.01)
        )
    for i in range(6):
        rows.append(
            (f"b-{i}", f"beta {i}", {"source": f"/b/{i}.md", "silo": "beta", "is_local": 1}, 0.50 + i * 0.01)
        )
    coll.rows = rows
    res = run_retrieve("alpha beta", n_results=6, db_path=db)
    silos = [c["silo"] for c in res["chunks"]]
    assert len(res["chunks"]) == 6
    assert silos.count("alpha") == 3
    assert silos.count("beta") == 3
