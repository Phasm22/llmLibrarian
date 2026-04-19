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


def test_add_silo_forwards_exclude_patterns(monkeypatch, tmp_path):
    target = tmp_path / "src"
    target.mkdir()
    seen = {}

    def _fake_run_ingest(request):
        seen["exclude_patterns"] = request.exclude_patterns
        from orchestration.ingest import IngestResult

        return IngestResult(files_indexed=1, failures=0, silo_slug="src")

    monkeypatch.setattr("orchestration.ingest.run_ingest", _fake_run_ingest)
    out = mcp_server.add_silo(path=str(target), exclude_patterns=["node_modules/", "*.tmp"])
    assert out["status"] == "started"
    # the background thread should get the same values once it runs
    import time

    for _ in range(50):
        if "exclude_patterns" in seen:
            break
        time.sleep(0.02)
    assert seen["exclude_patterns"] == ["node_modules/", "*.tmp"]
