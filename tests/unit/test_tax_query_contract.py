import pytest

from tax.query_contract import (
    METRIC_FEDERAL_WITHHELD,
    METRIC_PAYROLL_TAXES,
    METRIC_STATE_TAX,
    METRIC_TOTAL_INCOME,
    METRIC_WAGES,
    METRIC_W2_BOX,
    parse_tax_query,
)

TOP10_W2_REAL_LIFE_QUERIES = [
    "how much did i make in 2025",
    "how much did i make at acme in 2025",
    "what were my w-2 wages in 2025",
    "how much federal income tax was withheld in 2025",
    "how much did i pay in taxes at acme in 2025",
    "what were my payroll taxes in 2025",
    "what was my social security and medicare withholding in 2025",
    "how much state tax was withheld in 2025",
    "box 2 acme 2025",
    "box 17 acme 2025",
]

TOP10_W2_REAL_LIFE_EXPECTED_METRIC = {
    "how much did i make in 2025": METRIC_TOTAL_INCOME,
    "how much did i make at acme in 2025": METRIC_TOTAL_INCOME,
    "what were my w-2 wages in 2025": METRIC_WAGES,
    "how much federal income tax was withheld in 2025": METRIC_FEDERAL_WITHHELD,
    "how much did i pay in taxes at acme in 2025": METRIC_FEDERAL_WITHHELD,
    "what were my payroll taxes in 2025": METRIC_PAYROLL_TAXES,
    "what was my social security and medicare withholding in 2025": METRIC_PAYROLL_TAXES,
    "how much state tax was withheld in 2025": METRIC_STATE_TAX,
    "box 2 acme 2025": METRIC_W2_BOX,
    "box 17 acme 2025": METRIC_W2_BOX,
}


def test_parse_tax_query_make_with_year_and_employer():
    req = parse_tax_query("how much did i make in 2025 at ymca")
    assert req is not None
    assert req.metric == METRIC_TOTAL_INCOME
    assert req.tax_year == 2025
    assert req.employer == "YMCA"
    assert "ymca" in req.employer_tokens


def test_parse_tax_query_box_lookup():
    req = parse_tax_query("box 2 deloitte 2025")
    assert req is not None
    assert req.metric == METRIC_W2_BOX
    assert req.box_number == 2
    assert req.tax_year == 2025
    assert "deloitte" in req.employer_tokens


def test_parse_tax_query_taxes_paid_defaults_to_withheld():
    req = parse_tax_query("how much did i pay in taxes at deloitte in 2025")
    assert req is not None
    assert req.metric == METRIC_FEDERAL_WITHHELD
    assert req.tax_year == 2025
    assert req.interpretation is not None


def test_parse_tax_query_federal_witheld_for_employer():
    req = parse_tax_query("what was my federal wages witheld for deloitte in 2025")
    assert req is not None
    assert req.metric == METRIC_FEDERAL_WITHHELD
    assert req.tax_year == 2025
    assert "deloitte" in req.employer_tokens


def test_parse_tax_query_returns_none_for_non_tax_question():
    assert parse_tax_query("what was i coding in 2025") is None


@pytest.mark.parametrize("query", TOP10_W2_REAL_LIFE_QUERIES)
def test_parse_tax_query_top10_real_life_parses_and_has_year(query):
    req = parse_tax_query(query)
    assert req is not None
    assert req.tax_year == 2025


@pytest.mark.parametrize("query", TOP10_W2_REAL_LIFE_QUERIES)
def test_parse_tax_query_top10_real_life_metric_mapping_is_deterministic(query):
    req = parse_tax_query(query)
    assert req is not None
    assert req.metric == TOP10_W2_REAL_LIFE_EXPECTED_METRIC[query]


def test_parse_tax_query_top10_real_life_taxes_paid_includes_interpretation():
    req = parse_tax_query("how much did i pay in taxes at acme in 2025")
    assert req is not None
    assert req.metric == METRIC_FEDERAL_WITHHELD
    assert req.interpretation is not None
