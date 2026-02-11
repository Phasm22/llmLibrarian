from llmli_evals.adversarial import (
    build_query_suite,
    materialize_corpus,
    score_query,
    format_report_table,
    _split_answer_and_sources,
)


def test_materialize_corpus_creates_expected_volume(tmp_path):
    sources = materialize_corpus(tmp_path)
    assert len(sources) >= 30
    assert len(sources) <= 50
    assert any("canonical-rankings" in s for s in sources)
    assert any("official-rankings" in s for s in sources)
    assert any("executive-opinion" in s for s in sources)


def test_query_suite_has_fixed_size_and_categories():
    qs = build_query_suite()
    assert len(qs) == 60
    cats = {q["category"] for q in qs}
    assert cats == {"direct", "contradiction", "temporal", "adversarial"}


def test_split_answer_and_sources_parses_footer_format():
    output = (
        "Low confidence: ...\n\nAnswer body.\n\n---\nAnswered by: llmli\n\nSources:\n"
        "  • a.md (line 12) · 0.31\n"
        "    snippet\n"
        "  • b.md (line 3) · 0.20\n"
    )
    answer, sources = _split_answer_and_sources(output)
    assert "Answer body." in answer
    assert sources == ["a.md", "b.md"]


def test_score_query_correct_fact_passes():
    q = {
        "id": "QX",
        "query": "x",
        "category": "direct",
        "expected_type": "correct_fact",
        "expected_values": ["27"],
        "allowed_sources": ["truth.md"],
        "forbidden_values": ["34"],
        "time_rule": None,
    }
    r = score_query(q, "Aster SLA is 27 minutes.", ["truth.md"])
    assert r["passed"] is True
    assert r["subscores"]["factual_correctness"] == 1


def test_score_query_safe_abstain_requires_abstention():
    q = {
        "id": "QY",
        "query": "x",
        "category": "contradiction",
        "expected_type": "safe_abstain",
        "expected_values": [],
        "allowed_sources": ["a.md", "b.md"],
        "forbidden_values": ["4.2%", "6.8%"],
        "time_rule": None,
    }
    r = score_query(q, "I don't have enough evidence to say.", ["a.md", "b.md"])
    assert r["passed"] is True

    r2 = score_query(q, "It's definitely 4.2%.", ["a.md"])
    assert r2["passed"] is False
    assert "expected_abstain" in (r2["failure_reason"] or "")


def test_score_query_allows_conflict_mention_without_forbidden_hit():
    q = {
        "id": "QZ",
        "query": "x",
        "category": "direct",
        "expected_type": "correct_fact",
        "expected_values": ["27"],
        "allowed_sources": ["2025-02-15-canonical-ops-sla.md"],
        "forbidden_values": ["34"],
        "time_rule": None,
    }
    r = score_query(
        q,
        "There is a conflicting report with 34, but canonical says 27.",
        ["2025-02-15-canonical-ops-sla.md"],
    )
    assert "forbidden_value_hit" not in (r["failure_reason"] or "")


def test_score_query_direct_canonical_abstain_fails_decisive_rule():
    q = {
        "id": "QA",
        "query": "x",
        "category": "direct",
        "expected_type": "correct_fact",
        "expected_values": ["1"],
        "allowed_sources": ["2025-03-01-canonical-rankings.md"],
        "forbidden_values": ["2"],
        "time_rule": None,
    }
    r = score_query(
        q,
        "I don't have enough evidence to say.",
        ["2025-03-01-canonical-rankings.md", "2025-03-02-analyst-brief-contradiction.md"],
    )
    assert r["passed"] is False
    assert "expected_decisive_from_canonical" in (r["failure_reason"] or "")


def test_format_report_table_contains_key_metrics():
    report = {
        "run_id": "abc123",
        "model": "m",
        "silo": "__adversarial_eval__",
        "totals": {
            "total_queries": 2,
            "passed_queries": 1,
            "failed_queries": 1,
            "overall_trust_score": 50.0,
            "hallucination_rate": 0.0,
            "confident_error_rate": 0.0,
            "temporal_error_rate": 0.0,
            "unsupported_assertion_rate": 0.0,
        },
        "category_breakdown": {"direct": {"total": 2, "passed": 1, "failed": 1}},
        "failures": [
            {
                "query_id": "Q001",
                "category": "direct",
                "passed": False,
                "subscores": {
                    "factual_correctness": 0,
                    "trust_behavior": 0,
                    "evidence_grounding": 0,
                    "temporal_correctness": None,
                },
                "failure_reason": "missing_expected_value",
                "answer_text": "x",
                "sources_seen": [],
            }
        ],
        "records": [],
    }
    s = format_report_table(report)
    assert "Overall Trust Score: 50.0%" in s
    assert "Top Failures:" in s
