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


def test_execute_retrieve_chroma_phase_groups_photo_metadata(monkeypatch):
    class _FakeClient:
        def get_or_create_collection(self, **_kwargs):
            return object()

    photo_meta = {
        "source": "/photos/IMG_3083.JPG",
        "silo": "photos",
        "doc_type": "other",
        "record_type": "image_summary",
        "photo_taken_at": "2009-11-27T07:56:43",
        "camera_make": "Canon",
        "camera_model": "Canon PowerShot A1000 IS",
        "image_width": 3648,
        "image_height": 2736,
    }
    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_args, **_kwargs: (["Image summary: Trevi Fountain"], [photo_meta], [0.1], ["img1"], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kwargs: (kwargs["docs_v"], kwargs["metas_v"], kwargs["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())

    result = execute_retrieve_chroma_phase(
        db="/tmp/db",
        intent="LOOKUP",
        query="Trevi Fountain",
        query_for_retrieval="Trevi Fountain",
        silo_slug="photos",
        n_stage1=6,
        n_results=4,
        section=None,
        doc_type=None,
        db_path="/tmp/db",
        get_chroma_client=lambda _db: _FakeClient(),
    )

    assert result["chunks"][0]["photo_metadata"] == {
        "photo_taken_at": "2009-11-27T07:56:43",
        "camera_make": "Canon",
        "camera_model": "Canon PowerShot A1000 IS",
        "image_width": 3648,
        "image_height": 2736,
    }


def test_execute_retrieve_chroma_phase_merges_image_vector_results(monkeypatch):
    class _FakeClient:
        def get_or_create_collection(self, **kwargs):
            return object()

    text_meta = {"source": "/recipes/book.pdf", "silo": "recipes", "doc_type": "pdf"}
    image_meta = {
        "source": "/photos/bento.JPG",
        "silo": "photos",
        "doc_type": "other",
        "record_type": "image_summary",
        "source_modality": "image",
        "summary_status": "deferred",
        "needs_vision_enrichment": True,
    }
    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_args, **_kwargs: (["Bento box recipe"], [text_meta], [0.4], ["text1"], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kwargs: (kwargs["docs_v"], kwargs["metas_v"], kwargs["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked.get_image_embedding_adapter", lambda: object())
    monkeypatch.setattr(
        "query.retrieve_locked._query_image_collection",
        lambda **_kwargs: (["Image summary: bento meal"], [image_meta], [0.08]),
    )

    result = execute_retrieve_chroma_phase(
        db="/tmp/db",
        intent="LOOKUP",
        query="What restaurant did I take that Bento box picture at?",
        query_for_retrieval="restaurant Bento box picture",
        silo_slug=None,
        n_stage1=8,
        n_results=4,
        section=None,
        doc_type=None,
        db_path="/tmp/db",
        get_chroma_client=lambda _db: _FakeClient(),
    )

    assert result["image_search"] == {
        "attempted": True,
        "status": "ok",
        "matches": 1,
        "returned_chunks": 1,
    }
    assert result["retrieval_method"] == "image_vector"
    assert result["chunks"][0]["source"] == "/photos/bento.JPG"
    assert all(chunk["source"] != "/recipes/book.pdf" for chunk in result["chunks"])
    assert result["recommended_action"]["tool"] == "ask_image"
    assert result["recommended_action"]["files"] == ["/photos/bento.JPG"]


def test_execute_retrieve_chroma_phase_reports_unavailable_image_search(monkeypatch):
    class _FakeClient:
        def get_or_create_collection(self, **_kwargs):
            return object()

    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_args, **_kwargs: (["Bento recipe"], [{"source": "/book.pdf"}], [0.4], ["text1"], None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kwargs: (kwargs["docs_v"], kwargs["metas_v"], kwargs["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked.get_image_embedding_adapter", lambda: None)
    monkeypatch.setattr("query.retrieve_locked.image_embedding_unavailable_reason", lambda: "broken torchvision")

    result = execute_retrieve_chroma_phase(
        db="/tmp/db",
        intent="LOOKUP",
        query="find my Bento photo",
        query_for_retrieval="find my Bento photo",
        silo_slug=None,
        n_stage1=8,
        n_results=4,
        section=None,
        doc_type=None,
        db_path="/tmp/db",
        get_chroma_client=lambda _db: _FakeClient(),
    )

    assert result["image_search"]["status"] == "unavailable"
    assert "Do not treat text-only results" in result["image_search"]["warning"]
