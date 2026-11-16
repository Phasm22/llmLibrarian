"""section= filters the candidate pool before ranking cuts it, and never falls back."""
from __future__ import annotations

import mcp_contract
from query.retrieve_locked import execute_retrieve_chroma_phase


def _candidates() -> tuple[list[str], list[dict], list[float], list[str]]:
    docs, metas, dists, ids = [], [], [], []
    for i in range(8):
        section = "Item 1A. Risk Factors" if i == 6 else f"Unrelated {i}"
        docs.append(f"chunk {i}")
        metas.append({"source": f"/docs/f{i}.html", "silo": "docs", "doc_type": "other", "section": section})
        dists.append(0.1 + i * 0.05)
        ids.append(f"id{i}")
    return docs, metas, dists, ids


def _run(monkeypatch, section: str | None, n_results: int = 3, tags: str | None = None, rows=None) -> dict:
    class _FakeClient:
        def get_or_create_collection(self, **_kwargs):
            return object()

    docs, metas, dists, ids = rows or _candidates()
    monkeypatch.setattr(
        "query.retrieve_locked._safe_query",
        lambda *_a, **_k: (list(docs), [dict(m) for m in metas], list(dists), list(ids), None),
    )
    monkeypatch.setattr(
        "query.retrieve_locked.run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector_only"),
    )
    monkeypatch.setattr("query.retrieve_locked.get_embedding_function", lambda batch_size=1: object())
    monkeypatch.setattr("query.retrieve_locked._artifact_stream_enabled", lambda _db, _silo: False)
    return execute_retrieve_chroma_phase(
        db="/tmp/db",
        intent="LOOKUP",
        query="risk",
        query_for_retrieval="risk",
        silo_slug="docs",
        n_stage1=8,
        n_results=n_results,
        section=section,
        doc_type=None,
        db_path="/tmp/db",
        get_chroma_client=lambda _db: _FakeClient(),
        tags=tags,
    )


def test_matching_section_below_the_old_cutoff_is_returned(monkeypatch):
    result = _run(monkeypatch, "risk factors")
    assert [c["text"] for c in result["chunks"]] == ["chunk 6"]
    assert result["chunks"][0]["rank"] == 1


def test_missing_section_returns_no_unrelated_chunks(monkeypatch):
    result = _run(monkeypatch, "Item 7. MD&A")
    assert result["chunks"] == []
    assert result["section_filter"] == {"section": "Item 7. MD&A", "matched": 0, "candidates": 8}


def test_missing_section_guidance_drops_the_section(monkeypatch):
    result = _run(monkeypatch, "Item 7. MD&A")
    mcp_contract.apply_guidance(result, query="risk", silo="docs")
    action = result["recommended_action"]
    assert action["tool"] == "query_personal_knowledge"
    assert action["args"] == {"query": "risk", "silo": "docs"}
    assert "section" in action["reason"]


def test_no_section_keeps_existing_behavior(monkeypatch):
    result = _run(monkeypatch, None)
    assert [c["text"] for c in result["chunks"]] == ["chunk 0", "chunk 1", "chunk 2"]
    assert "section_filter" not in result


def test_tag_filter_sees_a_match_below_the_old_cutoff(monkeypatch):
    docs, metas, dists, ids = _candidates()
    metas[6]["tags"] = ",weeknight,"
    result = _run(monkeypatch, None, tags="weeknight", rows=(docs, metas, dists, ids))
    assert [c["text"] for c in result["chunks"]] == ["chunk 6"]
