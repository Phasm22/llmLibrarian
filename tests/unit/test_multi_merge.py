"""Merge properties every multi-query variant must keep."""
from __future__ import annotations

from query.multi_merge import merge_query_results

HEADER = "Weekly sync template. " * 12  # > 200 chars shared by every note


def _c(text, score=0.5, **kw):
    return {"text": text, "score": score, "source": kw.pop("source", "/s"), "silo": "w", **kw}


def test_distinct_chunks_sharing_a_200_char_prefix_both_survive():
    a, b = _c(HEADER + "decision A"), _c(HEADER + "decision B")
    merged, truncated, _m = merge_query_results([("qa", [a]), ("qb", [b])], 30)
    assert {c["text"][-10:] for c in merged} == {"decision A", "decision B"}
    assert truncated is False


def test_identical_text_collapses_once_and_keeps_provenance():
    shared = _c("same chunk")
    merged, _t, _m = merge_query_results([("q1", [shared]), ("q2", [dict(shared)])], 30)
    assert len(merged) == 1
    assert merged[0]["query"] in ("q1", "q2")


def test_empty_text_is_skipped_and_inputs_are_not_mutated():
    real = _c("real")
    merged, _t, _m = merge_query_results([("q", [_c(""), real])], 30)
    assert [c["text"] for c in merged] == ["real"]
    assert "query" not in real


def test_truncation_flag_and_cap():
    chunks = [_c(f"t{i}", 1.0 - i / 100) for i in range(8)]
    merged, truncated, _m = merge_query_results([("q", chunks)], 3)
    assert truncated is True and len(merged) == 3


def test_single_reranked_query_order_is_preserved():
    # A cross-encoder order is not score order; the merge must not undo it.
    chunks = [_c("r1", 0.2), _c("r2", 0.9), _c("r3", None)]
    merged, _t, method = merge_query_results([("q", chunks)], 30, reranked=True)
    assert method == "rrf"
    assert [c["text"] for c in merged] == ["r1", "r2", "r3"]


def test_without_rerank_legacy_score_order_is_kept():
    merged, _t, method = merge_query_results(
        [("q1", [_c("low", 0.1), _c("lex", None)]), ("q2", [_c("high", 0.9), _c("mid", 0.5)])], 30
    )
    assert method == "score_sort"
    assert [c["text"] for c in merged] == ["high", "mid", "low", "lex"]
    assert [c["query"] for c in merged] == ["q2", "q2", "q1", "q1"]


def test_rrf_rewards_agreement_over_raw_score():
    a, b = _c("A", 0.2), _c("B", 0.9)
    merged, _t, method = merge_query_results([("q1", [a]), ("q2", [b, dict(a)])], 30, reranked=True)
    assert method == "rrf"
    assert [c["text"] for c in merged] == ["A", "B"]
    assert merged[0]["query"] == "q1"


def test_rrf_does_not_sink_lexical_only_hits_without_distance():
    merged, _t, _m = merge_query_results([("q", [_c("lex", None), _c("vec", 0.9)])], 30, reranked=True)
    assert [c["text"] for c in merged] == ["lex", "vec"]


def test_rrf_provenance_is_best_rank():
    x = _c("X")
    merged, _t, _m = merge_query_results([("q1", [_c("p"), _c("q"), x]), ("q2", [dict(x)])], 30, reranked=True)
    assert next(c for c in merged if c["text"] == "X")["query"] == "q2"


def test_cli_multi_query_uses_the_same_merge(monkeypatch, tmp_path):
    from cli_agent import run_multi_query

    header = "Trip report template. " * 12
    by_query = {
        "maine": {"rerank": {"status": "applied"}, "chunks": [_c(header + "Maine cabin", 0.2), _c("noise", 0.8)]},
        "denver": {"rerank": {"status": "applied"}, "chunks": [_c(header + "Denver friends", 0.3)]},
    }
    monkeypatch.setattr("query.core.run_retrieve", lambda **kw: by_query[kw["query"]])
    res = run_multi_query(["maine", "denver"], db_path=tmp_path, max_total_chunks=2)
    assert res["merge_method"] == "rrf"
    assert res["truncated"] is True
    assert [c["query"] for c in res["chunks"]] == ["maine", "denver"]
    assert all(c["text"].startswith(header) for c in res["chunks"])
