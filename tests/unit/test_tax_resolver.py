from query.tax_resolver import run_tax_resolver


def _base_row(**overrides):
    row = {
        "record_id": "r1",
        "silo": "tax",
        "source": "/Users/x/Tax/2025/ymca-w2-2025.pdf",
        "page": 1,
        "doc_hash": "h1",
        "tax_year": 2025,
        "form_type": "W2",
        "field_code": "w2_box_1_wages",
        "field_label": "W-2 Box 1 wages",
        "entity_name": "YMCA",
        "entity_tokens": ["ymca"],
        "raw_value": "4,626.76",
        "normalized_decimal": "4626.76",
        "currency": "USD",
        "extractor_tier": "ocr_layout",
        "confidence": 0.80,
        "trace_ref": "x#page=1",
        "created_at": "2026-02-21T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_tax_resolver_returns_single_employer_value(monkeypatch):
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: [_base_row()],
    )
    out = run_tax_resolver(
        query="how much did i make in 2025 at ymca",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "Total income at YMCA (2025)" in out["response"]


def test_tax_resolver_abstains_on_conflicting_values(monkeypatch):
    rows = [
        _base_row(record_id="r1", normalized_decimal="4626.76", raw_value="4,626.76"),
        _base_row(record_id="r2", source="/Users/x/Tax/2025/ymca-copy.pdf", normalized_decimal="4700.00", raw_value="4,700.00"),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="box 1 ymca 2025",
        intent="TAX_QUERY",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert "Abstain [conflict]" in out["response"]


def test_tax_resolver_employer_not_found_is_no_match(monkeypatch):
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: [_base_row(entity_tokens=["acme"], entity_name="ACME")],
    )
    out = run_tax_resolver(
        query="how much did i make in 2025 at ymca",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    # MONEY_YEAR_TOTAL path falls back when ledger has no usable rows for employer.
    assert out is not None
    assert out["guardrail_no_match"] is True


def test_tax_resolver_no_rows_returns_none_for_compat_on_non_tax_intent(monkeypatch):
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: [],
    )
    out = run_tax_resolver(
        query="how much did i make in 2025",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is None


def test_tax_resolver_no_rows_abstains_for_tax_query_intent(monkeypatch):
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: [],
    )
    out = run_tax_resolver(
        query="box 2 deloitte 2025",
        intent="TAX_QUERY",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert "Abstain [no_match]" in out["response"]
