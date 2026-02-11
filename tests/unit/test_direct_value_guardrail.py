from query.guardrails import (
    extract_numeric_or_key_values_from_query,
    extract_candidate_value_pairs_from_context,
    select_consistent_value,
    run_direct_value_consistency_guardrail,
)


def test_extract_numeric_or_key_values_from_query_detects_direct_metric():
    s = extract_numeric_or_key_values_from_query("What is Aster Grill operating margin?")
    assert s is not None
    assert "aster" in s["query_tokens"]
    assert "grill" in s["query_tokens"]
    assert "margin" in s["metric_tokens"]


def test_extract_candidate_value_pairs_from_context_finds_colon_values():
    signals = extract_numeric_or_key_values_from_query("What is Aster Grill operating margin?")
    docs = [
        "Aster Grill operating margin: 18.2%",
        "Blue Harbor operating margin: 17.1%",
    ]
    metas = [{"source": "/tmp/2025-04-05-canonical-margin.md"}, {"source": "/tmp/2025-04-05-canonical-margin.md"}]
    cands = extract_candidate_value_pairs_from_context(docs, metas, signals or {})
    assert any(c["value"] == "18.2%" for c in cands)


def test_extract_candidate_value_pairs_from_context_filters_unrelated_metric_lines():
    signals = extract_numeric_or_key_values_from_query("Ember Table NPS in 2025?")
    docs = [
        "Ember Table revenue rank: 3",
        "Ember Table NPS: 62",
    ]
    metas = [
        {"source": "/tmp/2025-03-01-canonical-rankings.md"},
        {"source": "/tmp/2025-01-20-canonical-nps.md"},
    ]
    cands = extract_candidate_value_pairs_from_context(docs, metas, signals or {})
    values = {c["value"] for c in cands}
    assert "62" in values
    assert "3" not in values


def test_select_consistent_value_prefers_canonical():
    cands = [
        {
            "label": "Aster Grill operating margin",
            "value": "14.9%",
            "meta": {"source": "/tmp/2025-04-06-margin-contradiction.md"},
            "doc": "Aster Grill operating margin: 14.9%",
            "overlap": 3,
        },
        {
            "label": "Aster Grill operating margin",
            "value": "18.2%",
            "meta": {"source": "/tmp/2025-04-05-canonical-margin.md"},
            "doc": "Aster Grill operating margin: 18.2%",
            "overlap": 3,
        },
    ]
    selected = select_consistent_value(cands, ["canonical", "official"], ["draft", "archive"])
    assert selected is not None
    assert selected["status"] == "selected"
    assert selected["value"] == "18.2%"


def test_run_direct_value_consistency_guardrail_abstains_on_equal_conflict():
    docs = [
        "Fjord Bistro monthly churn: 4.2%",
        "Fjord Bistro monthly churn: 6.8%",
    ]
    metas = [
        {"source": "/tmp/2025-05-15-equal-conflict-a.md"},
        {"source": "/tmp/2025-05-15-equal-conflict-b.md"},
    ]
    out = run_direct_value_consistency_guardrail(
        query="What's the exact Fjord Bistro churn?",
        docs=docs,
        metas=metas,
        source_label="llmli",
        no_color=True,
        canonical_tokens=["canonical", "official"],
        deprioritized_tokens=["draft", "archive"],
    )
    assert out is not None
    assert "don't have enough evidence" in out["response"].lower()
    assert out["guardrail_reason"] == "direct_value_equal_conflict"


def test_select_consistent_value_returns_abstain_on_equal_conflict_without_canonical():
    cands = [
        {
            "label": "Aster Grill operating margin",
            "value": "18.2%",
            "meta": {"source": "/tmp/2025-04-05-margin-notes.md"},
            "doc": "Aster Grill operating margin: 18.2%",
            "overlap": 1,
        },
        {
            "label": "Aster Grill operating margin",
            "value": "17.9%",
            "meta": {"source": "/tmp/2025-04-06-margin-notes.md"},
            "doc": "Aster Grill operating margin: 17.9%",
            "overlap": 1,
        },
    ]
    selected = select_consistent_value(cands, ["canonical", "official"], ["draft", "archive"])
    assert selected is not None
    assert selected["status"] == "abstain_equal_conflict"
