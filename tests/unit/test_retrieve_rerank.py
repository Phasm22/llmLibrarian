"""Cross-encoder placement in tool retrieval (execute_retrieve_chroma_phase), mocked model."""
from __future__ import annotations

import sys

import pytest

import reranker
from query.retrieve_locked import execute_retrieve_chroma_phase

POOL = [f"doc-{i}" for i in range(12)]


class _Model:
    def __init__(self, best: str | None = None, fail: bool = False):
        self.best = best
        self.fail = fail
        self.calls: list[int] = []

    def predict(self, pairs):
        if self.fail:
            raise RuntimeError("model missing")
        self.calls.append(len(pairs))
        return [10.0 if p[1] == self.best else -float(POOL.index(p[1])) for p in pairs]


def _install(monkeypatch, model: _Model, silos=("docs",)):
    metas = [
        {"source": f"/f/{i % 6}.md", "silo": silos[i % len(silos)], "chunk_hash": f"h{i}"} for i in range(len(POOL))
    ]
    dists = [0.1 + 0.02 * i for i in range(len(POOL))]
    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_a, **_k: (list(POOL), [dict(m) for m in metas], list(dists), [f"id{i}" for i in range(len(POOL))], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked.private_filter_clause", lambda _db: None)
    monkeypatch.setattr(reranker, "_get_model", lambda _n, _d: model)


class _Client:
    def get_or_create_collection(self, **_kw):
        return object()


def _run(n_results: int, status: str = "eligible", silo: str | None = "docs") -> dict:
    return execute_retrieve_chroma_phase(
        db="/tmp/db", intent="LOOKUP", query="q", query_for_retrieval="q", silo_slug=silo,
        n_stage1=len(POOL), n_results=n_results, section=None, doc_type=None, db_path="/tmp/db",
        get_chroma_client=lambda _db: _Client(), rerank_status=status,
    )


def test_disabled_and_year_skipped_never_call_the_model(monkeypatch):
    model = _Model(best="doc-6")
    _install(monkeypatch, model)
    for status in ("disabled", "year_skipped"):
        res = _run(8, status=status)
        assert res["rerank"]["status"] == status
        assert [c["text"] for c in res["chunks"]][:2] == ["doc-0", "doc-1"]
    assert model.calls == []


def test_promotes_candidate_six_and_keeps_distance_aligned(monkeypatch):
    _install(monkeypatch, _Model(best="doc-6"))
    res = _run(8)
    assert res["rerank"]["status"] == "applied"
    first = res["chunks"][0]
    assert first["text"] == "doc-6"
    assert first["score"] == round(1.0 - (0.1 + 0.02 * 6), 4)


def test_failure_keeps_retrieval_order_and_says_so(monkeypatch):
    _install(monkeypatch, _Model(fail=True))
    res = _run(5)
    assert res["rerank"]["status"] == "failed"
    assert [c["text"] for c in res["chunks"]] == POOL[:5]


def test_backfills_from_beyond_the_output_window(monkeypatch):
    _install(monkeypatch, _Model(best="doc-9"))
    res = _run(3)
    assert res["chunks"][0]["text"] == "doc-9"


def test_unscoped_diversity_still_fills_available_slots(monkeypatch):
    _install(monkeypatch, _Model(best="doc-7"), silos=("a", "b", "c"))
    res = _run(6, silo=None)
    assert res["chunks"][0]["text"] == "doc-7"
    assert len(res["chunks"]) == 6


def _ask_context(monkeypatch, mock_collection, mock_ollama, model, n_results=8, query="what is alpha", sources=None):
    from query.core import run_ask
    from query.intent import INTENT_LOOKUP

    monkeypatch.setattr("query.core.get_embedding_function", lambda **_kw: None)
    monkeypatch.setattr("query.core.get_client", lambda db_path: type("C", (), {"get_or_create_collection": lambda self, **k: mock_collection})())
    monkeypatch.setattr("query.core.route_intent", lambda _q: INTENT_LOOKUP)
    monkeypatch.setattr(reranker, "_get_model", lambda _n, _d: model)
    docs = [f"{d} context about alpha number {i}" for i, d in enumerate(POOL[:10])]
    pool = {d: i for i, d in enumerate(docs)}
    def _predict(pairs):
        model.calls.append(len(pairs))
        return [10.0 if p[1].startswith(model.best + " ") else -float(pool[p[1]]) for p in pairs]

    model.predict = _predict
    mock_collection.query_result = {
        "documents": [docs],
        "metadatas": [[{"source": (sources or {}).get(i, f"/tmp/{i}.txt"), "is_local": 1, "silo": "s1" if i < 5 else f"s{i}"} for i in range(10)]],
        "distances": [[0.1 + 0.01 * i for i in range(10)]],
        "ids": [[f"id-{i}" for i in range(10)]],
    }
    run_ask(archetype_id=None, query=query, no_color=True, use_reranker=True, n_results=n_results)
    return mock_ollama["calls"][-1]["messages"][1]["content"]


def test_ask_rerank_promotes_candidate_six(monkeypatch, mock_collection, mock_ollama):
    ctx = _ask_context(monkeypatch, mock_collection, mock_ollama, _Model(best="doc-6"))
    assert ctx.index("doc-6 context") < ctx.index("doc-0 context")


def test_ask_year_query_skips_rerank(monkeypatch, mock_collection, mock_ollama):
    model = _Model(best="doc-6")
    ctx = _ask_context(monkeypatch, mock_collection, mock_ollama, model, query="what is alpha in 2023")
    assert model.calls == []
    assert "doc-6 context" not in ctx or ctx.index("doc-0 context") < ctx.index("doc-6 context")


def test_ask_rerank_leaves_room_for_diversity_backfill(monkeypatch, mock_collection, mock_ollama):
    # Top five all come from one file; LOOKUP keeps three per file, so slots four
    # and five must come from below the rerank cut.
    big = {i: "/tmp/big.txt" for i in range(5)}
    ctx = _ask_context(monkeypatch, mock_collection, mock_ollama, _Model(best="doc-0"), n_results=5, sources=big)
    assert "doc-5 context" in ctx and "doc-6 context" in ctx


def test_head_bound_limits_cross_encoder_work(monkeypatch):
    model = _Model(best="doc-30")
    monkeypatch.setattr(sys.modules[__name__], "POOL", [f"doc-{i}" for i in range(40)])
    _install(monkeypatch, model)
    res = _run(5)
    assert model.calls == [reranker.rerank_head_size(5)] == [20]
    assert "doc-30" not in [c["text"] for c in res["chunks"]]


def test_duplicate_hash_does_not_shrink_the_result(monkeypatch):
    docs = ["a", "a-dup", "b", "c"]
    metas = [
        {"source": "/f.md", "silo": "docs", "chunk_hash": "h1"},
        {"source": "/f.md", "silo": "docs", "chunk_hash": "h1"},
        {"source": "/f.md", "silo": "docs", "chunk_hash": "h2"},
        {"source": "/f.md", "silo": "docs", "chunk_hash": "h3"},
    ]
    dists = [0.1, 0.11, 0.2, 0.3]
    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_a, **_k: (docs, [dict(m) for m in metas], list(dists), ["1", "2", "3", "4"], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda *_a: False)
    res = execute_retrieve_chroma_phase(
        db="/tmp/db", intent="LOOKUP", query="q", query_for_retrieval="q", silo_slug="docs",
        n_stage1=4, n_results=3, section=None, doc_type=None, db_path="/tmp/db",
        get_chroma_client=lambda _db: _Client(), rerank_status="disabled",
    )
    assert [c["text"] for c in res["chunks"]] == ["a", "b", "c"]


@pytest.mark.parametrize("section", [None, "target"])
def test_small_named_file_comes_back_in_document_order(monkeypatch, section):
    ranked = ["second paragraph", "first paragraph"]
    metas = [
        {"source": "/n.md", "silo": "docs", "chunk_hash": "b", "chunk_index": 1, "line_start": 2, "section": "target"},
        {"source": "/n.md", "silo": "docs", "chunk_hash": "a", "chunk_index": 0, "line_start": 1, "section": "other"},
    ]
    stored = [
        ("first paragraph", {"source": "/n.md", "silo": "docs", "chunk_hash": "a", "chunk_index": 0, "line_start": 1, "section": "other"}),
        ("second paragraph", {"source": "/n.md", "silo": "docs", "chunk_hash": "b", "chunk_index": 1, "line_start": 2, "section": "target"}),
    ]

    class _Rows:
        def get(self, *, where, include, limit, offset):
            page = stored[offset : offset + limit]
            return {"documents": [d for d, _m in page], "metadatas": [m for _d, m in page]}

    class _RowsClient:
        def get_or_create_collection(self, **_kw):
            return _Rows()

    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_a, **_k: (ranked, [dict(m) for m in metas], [0.1, 0.2], ["2", "1"], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda *_a: False)
    res = execute_retrieve_chroma_phase(
        db="/tmp/db", intent="LOOKUP", query="q", query_for_retrieval="q", silo_slug="docs",
        n_stage1=4, n_results=2, section=section, doc_type=None, db_path="/tmp/db", source="/n.md",
        get_chroma_client=lambda _db: _RowsClient(), rerank_status="disabled",
    )
    if section:
        assert not res.get("whole_file")
        assert [c["text"] for c in res["chunks"]] == ["second paragraph"]
        return
    assert res["whole_file"] is True
    assert [c["text"] for c in res["chunks"]] == ["first paragraph", "second paragraph"]
    assert res["source_file"] == {
        "total_chunks": 2,
        "total_chars": len("first paragraph") + len("second paragraph"),
    }
