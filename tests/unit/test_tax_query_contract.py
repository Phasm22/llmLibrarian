from tax.query_contract import (
    METRIC_FEDERAL_WITHHELD,
    METRIC_TOTAL_INCOME,
    METRIC_W2_BOX,
    parse_tax_query,
)


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


def test_parse_tax_query_returns_none_for_non_tax_question():
    assert parse_tax_query("what was i coding in 2025") is None
