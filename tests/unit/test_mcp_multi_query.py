"""Coverage for multi_query_knowledge merge/dedup/truncate/sort/error logic.

The tool fans queries out to `run_retrieve`, then merges chunks by a
text-prefix dedup key, sorts by score, caps at max_total_chunks, and
collects per-query errors without aborting. These are the contract
properties the LLM caller depends on, so we lock them down here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    # Avoid touching the real chroma client between calls.
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    return mcp_server


def _install_run_retrieve(monkeypatch, by_query):
    """Stub query.core.run_retrieve to return a canned mapping query→result."""
    def _fake_run_retrieve(*, query, **_kwargs):
        return by_query[query]

    monkeypatch.setitem(
        sys.modules,
        "query.core",
        SimpleNamespace(run_retrieve=_fake_run_retrieve),
    )


def test_multi_query_db_missing(monkeypatch, tmp_path):
    import mcp_server
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(tmp_path / "missing"))

    res = mcp_server.multi_query_knowledge(["a", "b"])

    assert res["error"].startswith("LLMLIBRARIAN_DB does not exist")
    assert res["queries"] == ["a", "b"]
    assert res["total_chunks"] == 0
    assert res["chunks"] == []


def test_multi_query_merges_dedups_and_tags_query(monkeypatch, mcp):
    _install_run_retrieve(
        monkeypatch,
        {
            "alpha": {
                "chunks": [
                    {"text": "shared chunk text", "score": 0.9, "source": "/a"},
                    {"text": "alpha-only", "score": 0.7, "source": "/a"},
                ]
            },
            "beta": {
                "chunks": [
                    {"text": "shared chunk text", "score": 0.95, "source": "/a"},
                    {"text": "beta-only", "score": 0.6, "source": "/b"},
                ]
            },
        },
    )

    res = mcp.multi_query_knowledge(["alpha", "beta"])

    assert res["total_chunks"] == 3, "shared chunk should dedup"
    texts = [c["text"] for c in res["chunks"]]
    assert texts.count("shared chunk text") == 1
    # The first occurrence wins for query tagging (alpha runs first).
    shared = next(c for c in res["chunks"] if c["text"] == "shared chunk text")
    assert shared["query"] == "alpha"


def test_multi_query_sorts_by_score_desc(monkeypatch, mcp):
    _install_run_retrieve(
        monkeypatch,
        {
            "q1": {"chunks": [{"text": "low", "score": 0.1}]},
            "q2": {"chunks": [{"text": "high", "score": 0.9}]},
            "q3": {"chunks": [{"text": "mid", "score": 0.5}]},
        },
    )

    res = mcp.multi_query_knowledge(["q1", "q2", "q3"])
    scores = [c["score"] for c in res["chunks"]]
    assert scores == sorted(scores, reverse=True)
    assert res["chunks"][0]["text"] == "high"


def test_multi_query_truncates_at_max_total_chunks(monkeypatch, mcp):
    big_chunks = [{"text": f"text-{i}", "score": 1.0 - i * 0.001} for i in range(8)]
    _install_run_retrieve(monkeypatch, {"q": {"chunks": big_chunks}})

    res = mcp.multi_query_knowledge(["q"], max_total_chunks=3)

    assert res["truncated"] is True
    assert res["total_chunks"] == 3
    assert len(res["chunks"]) == 3


def test_multi_query_no_truncation_when_under_cap(monkeypatch, mcp):
    _install_run_retrieve(monkeypatch, {"q": {"chunks": [{"text": "only", "score": 0.5}]}})

    res = mcp.multi_query_knowledge(["q"], max_total_chunks=10)

    assert res["truncated"] is False
    assert res["total_chunks"] == 1


def test_multi_query_per_query_errors_are_collected(monkeypatch, mcp):
    def _fake_run_retrieve(*, query, **_kwargs):
        if query == "boom":
            raise RuntimeError("mock failure")
        return {"chunks": [{"text": "ok", "score": 0.5}]}

    monkeypatch.setitem(
        sys.modules,
        "query.core",
        SimpleNamespace(run_retrieve=_fake_run_retrieve),
    )

    res = mcp.multi_query_knowledge(["good", "boom", "good2"])

    assert res["total_chunks"] == 1, "good and good2 share dedup key"
    assert "errors" in res
    assert len(res["errors"]) == 1
    assert "boom" in res["errors"][0]
    assert "RuntimeError" in res["errors"][0]


def test_multi_query_empty_chunks_returns_empty_chunks(monkeypatch, mcp):
    _install_run_retrieve(monkeypatch, {"q": {"chunks": []}})

    res = mcp.multi_query_knowledge(["q"])

    assert res["total_chunks"] == 0
    assert res["chunks"] == []
    assert res["truncated"] is False
    # answer_confidence helper still runs; should report low/no-chunk note.
    assert res["answer_confidence"] == "low"


def test_multi_query_empty_text_chunk_skipped(monkeypatch, mcp):
    """Dedup key uses text[:200] — empty text yields empty key, which is
    explicitly skipped to avoid collapsing empties together."""
    _install_run_retrieve(
        monkeypatch,
        {"q": {"chunks": [{"text": "", "score": 0.5}, {"text": "real", "score": 0.4}]}},
    )

    res = mcp.multi_query_knowledge(["q"])

    texts = [c["text"] for c in res["chunks"]]
    assert "" not in texts
    assert "real" in texts


def test_multi_query_dedup_uses_first_200_chars(monkeypatch, mcp):
    """Two chunks that share the first 200 chars should dedup as one."""
    base = "x" * 250
    _install_run_retrieve(
        monkeypatch,
        {"q": {"chunks": [
            {"text": base + "AAA", "score": 0.9},
            {"text": base + "BBB", "score": 0.8},
        ]}},
    )

    res = mcp.multi_query_knowledge(["q"])
    assert res["total_chunks"] == 1
