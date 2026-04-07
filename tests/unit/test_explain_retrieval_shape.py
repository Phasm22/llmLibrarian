"""MCP explain_retrieval response includes explicit hybrid counts."""

import mcp_server


def test_explain_retrieval_hybrid_counts(monkeypatch):
    def fake_run_retrieve(**kwargs):
        return {
            "chunks": [
                {
                    "_signals": {"vector_rank": 1, "lexical_rank": None, "rrf_score": 0.5},
                    "rank": 1,
                    "score": 0.9,
                    "source": "a.py",
                    "silo": "s",
                    "text": "alpha",
                },
                {
                    "_signals": {"vector_rank": 2, "lexical_rank": 1, "rrf_score": 0.4},
                    "rank": 2,
                    "score": 0.8,
                    "source": "b.py",
                    "silo": "s",
                    "text": "beta",
                },
            ],
            "intent": "LOOKUP",
            "retrieval_method": "hybrid",
        }

    monkeypatch.setattr("query.core.run_retrieve", fake_run_retrieve)
    out = mcp_server.explain_retrieval(query="q", silo=None, n_results=20)
    assert out.get("error") is None
    assert out["lexical_hit_count"] == 1
    assert out["vector_only_chunk_count"] == 1
    assert out["chunk_with_vector_rank_count"] == 2
    assert out["vector_hit_count"] == 1
    assert "vector_rank" in out["signal_summary"]
    assert "semantic-only" in out["signal_summary"]


def test_explain_retrieval_vector_only_summary(monkeypatch):
    def fake_run_retrieve(**kwargs):
        return {
            "chunks": [
                {
                    "_signals": {"vector_rank": 1, "lexical_rank": None},
                    "rank": 1,
                    "score": 0.9,
                    "source": "a.py",
                    "silo": "s",
                    "text": "x",
                },
            ],
            "intent": "LOOKUP",
            "retrieval_method": "vector_only",
        }

    monkeypatch.setattr("query.core.run_retrieve", fake_run_retrieve)
    out = mcp_server.explain_retrieval(query="q", silo=None, n_results=20)
    assert out.get("error") is None
    assert out["lexical_hit_count"] == 0
    assert out["vector_only_chunk_count"] == 1
    assert out["chunk_with_vector_rank_count"] == 1
