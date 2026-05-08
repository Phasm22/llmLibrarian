from __future__ import annotations

from query.retrieval import merge_dual_streams_rrf
from query.retrieve_locked import execute_retrieve_chroma_phase


def test_merge_dual_streams_rrf_combines_both_streams():
    docs, metas, dists = merge_dual_streams_rrf(
        ["raw-one", "raw-two"],
        [{"source": "raw1"}, {"source": "raw2"}],
        [0.2, 0.3],
        ["artifact-one"],
        [{"source": "artifact1"}],
        [0.1],
        top_k=3,
    )
    assert len(docs) == 3
    sources = {str((m or {}).get("source") or "") for m in metas}
    assert "raw1" in sources
    assert "artifact1" in sources


def test_execute_retrieve_chroma_phase_uses_dual_stream_for_scoped_silo(monkeypatch):
    class _FakeCollection:
        def query(self, **kwargs):
            where = kwargs.get("where") or {}
            where_text = str(where)
            if "docs-artifacts" in where_text:
                return {
                    "documents": [["artifact chunk"]],
                    "metadatas": [[{"source": "artifact.txt", "silo": "docs-artifacts", "doc_type": "artifact"}]],
                    "distances": [[0.1]],
                    "ids": [["aid1"]],
                }
            return {
                "documents": [["raw chunk"]],
                "metadatas": [[{"source": "raw.txt", "silo": "docs", "doc_type": "other"}]],
                "distances": [[0.2]],
                "ids": [["rid1"]],
            }

    class _FakeClient:
        def get_or_create_collection(self, **_kwargs):
            return _FakeCollection()

    def _fake_safe_query(_collection, query_kw, _silo_slug, db_path=None):
        where = query_kw.get("where") or {}
        where_text = str(where)
        if "docs-artifacts" in where_text:
            return (
                ["artifact chunk"],
                [{"source": "artifact.txt", "silo": "docs-artifacts", "doc_type": "artifact"}],
                [0.1],
                ["aid1"],
                None,
            )
        return (
            ["raw chunk"],
            [{"source": "raw.txt", "silo": "docs", "doc_type": "other"}],
            [0.2],
            ["rid1"],
            None,
        )

    monkeypatch.setattr("query.retrieve_locked._safe_query", _fake_safe_query)
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda _db, _silo: True)
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kwargs: (kwargs["docs_v"], kwargs["metas_v"], kwargs["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())

    result = execute_retrieve_chroma_phase(
        db="/tmp/db",
        intent="LOOKUP",
        query="revenue",
        query_for_retrieval="revenue",
        silo_slug="docs",
        n_stage1=6,
        n_results=4,
        section=None,
        doc_type=None,
        db_path="/tmp/db",
        get_chroma_client=lambda _db: _FakeClient(),
    )
    assert result["retrieval_method"] == "dual_stream_rrf"
    silos = {chunk["silo"] for chunk in result["chunks"]}
    assert "docs" in silos
    assert "docs-artifacts" in silos
