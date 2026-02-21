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
        lambda db_path, silo=None, tax_year=None: [
            _base_row(
                source="/Users/x/Tax/2025/acme-w2-2025.pdf",
                entity_tokens=["acme"],
                entity_name="ACME",
            )
        ],
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


def test_tax_resolver_matches_employer_from_source_token_with_year_suffix(monkeypatch):
    rows = [
        _base_row(
            field_code="w2_box_2_federal_income_tax_withheld",
            field_label="W-2 Box 2 federal income tax withheld",
            normalized_decimal="4723.31",
            raw_value="4,723.31",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            entity_tokens=["deloitte2025"],
            entity_name="Colorado Deloitte2025",
        )
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
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
    assert out["guardrail_no_match"] is False
    assert "4,723.31" in out["response"]


def test_tax_resolver_total_income_ignores_w2_box_number_artifact_conflicts(monkeypatch):
    rows = [
        _base_row(
            record_id="d1",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="1.00",
            raw_value="1",
            confidence=0.85,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
        _base_row(
            record_id="d2",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="35676.33",
            raw_value="35,676.33",
            confidence=0.85,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
        _base_row(
            record_id="d3",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="4723.31",
            raw_value="4,723.31",
            confidence=0.90,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
        _base_row(
            record_id="y1",
            source="/Users/x/Tax/2025/ymca-w2-2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="2.00",
            raw_value="2",
            confidence=0.74,
            entity_tokens=["ymca"],
            entity_name="YMCA",
        ),
        _base_row(
            record_id="y2",
            source="/Users/x/Tax/2025/ymca-w2-2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="4626.76",
            raw_value="4,626.76",
            confidence=0.69,
            entity_tokens=["ymca"],
            entity_name="YMCA",
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much did i make in total in 2025",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "40,303.09" in out["response"]


def test_tax_resolver_box2_conflict_uses_box1_context(monkeypatch):
    rows = [
        _base_row(
            record_id="w1",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="35676.33",
            raw_value="35,676.33",
            confidence=0.85,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
        _base_row(
            record_id="b2a",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_2_federal_income_tax_withheld",
            field_label="W-2 Box 2 federal income tax withheld",
            normalized_decimal="37234.05",
            raw_value="37,234.05",
            confidence=0.90,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
        _base_row(
            record_id="b2b",
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_2_federal_income_tax_withheld",
            field_label="W-2 Box 2 federal income tax withheld",
            normalized_decimal="4723.31",
            raw_value="4,723.31",
            confidence=0.85,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
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
    assert out["guardrail_no_match"] is False
    assert "4,723.31" in out["response"]


def test_tax_resolver_sources_emit_osc_links_when_tty(monkeypatch):
    rows = [
        _base_row(
            source="/Users/x/Tax/2025/Federal_Colorado_deloitte2025.pdf",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="35676.33",
            raw_value="35,676.33",
            confidence=0.90,
            entity_tokens=["deloitte"],
            entity_name="Deloitte",
        )
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    monkeypatch.setattr("style.sys.stdout.isatty", lambda: True)

    out = run_tax_resolver(
        query="how much did i make in total in 2025",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=False,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "Sources:" in out["response"]
    assert "\033]8;;" in out["response"]


def test_tax_resolver_1040_line9_artifact_values_abstain_no_match(monkeypatch):
    rows = [
        _base_row(
            record_id="r1",
            source="/Users/x/Tax/2022/2022_TaxReturn.pdf",
            tax_year=2022,
            form_type="1040",
            field_code="f1040_line_9_total_income",
            field_label="Form 1040 line 9 total income",
            normalized_decimal="7.00",
            raw_value="7",
            confidence=0.90,
        ),
        _base_row(
            record_id="r2",
            source="/Users/x/Tax/2022/2022_TaxReturn.pdf",
            tax_year=2022,
            form_type="1040",
            field_code="f1040_line_9_total_income",
            field_label="Form 1040 line 9 total income",
            normalized_decimal="10.00",
            raw_value="10",
            confidence=0.90,
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much did i make in total in 2022",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert "Abstain [no_match]" in out["response"]


def test_tax_resolver_conflict_message_includes_candidates(monkeypatch):
    rows = [
        _base_row(
            record_id="c1",
            source="/Users/x/Tax/2021/2021_TaxReturn.pdf",
            tax_year=2021,
            form_type="1040",
            field_code="f1040_line_9_total_income",
            field_label="Form 1040 line 9 total income",
            normalized_decimal="12000.00",
            raw_value="12,000.00",
            confidence=0.90,
        ),
        _base_row(
            record_id="c2",
            source="/Users/x/Tax/2021/2021_TaxReturn.pdf",
            tax_year=2021,
            form_type="1040",
            field_code="f1040_line_9_total_income",
            field_label="Form 1040 line 9 total income",
            normalized_decimal="15000.00",
            raw_value="15,000.00",
            confidence=0.90,
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much did i make in total in 2021",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert "Abstain [conflict]" in out["response"]
    assert "Candidates:" in out["response"]
    assert "12000.00" in out["response"]


def test_tax_resolver_ignores_w2_box1_year_artifact(monkeypatch):
    rows = [
        _base_row(
            record_id="y1",
            source="/Users/x/Tax/2022/2022_TaxReturn.pdf",
            tax_year=2022,
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="2022.00",
            raw_value="2022",
            confidence=0.90,
            form_type="W2",
        )
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much did i make in total in 2022",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is True
    assert "Abstain [no_match]" in out["response"]


def test_tax_resolver_ignores_1040_header_artifact_1040(monkeypatch):
    rows = [
        _base_row(
            record_id="h1",
            source="/Users/x/Tax/2020/2020_TaxReturn.pdf",
            tax_year=2020,
            form_type="1040",
            field_code="f1040_line_9_total_income",
            field_label="Form 1040 line 9 total income",
            normalized_decimal="1040.00",
            raw_value="1040",
            confidence=0.90,
        ),
        _base_row(
            record_id="w1",
            source="/Users/x/Tax/2020/W-2 2020.pdf",
            tax_year=2020,
            form_type="W2",
            field_code="w2_box_1_wages",
            field_label="W-2 Box 1 wages",
            normalized_decimal="15128.97",
            raw_value="15,128.97",
            confidence=0.72,
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much did i make in total in 2020",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "15,128.97" in out["response"]


def test_tax_resolver_interest_income_uses_1099_int_rows(monkeypatch):
    rows = [
        _base_row(
            record_id="i1",
            source="/Users/x/Tax/2025/TaxStatement_2025_1099INT_1.pdf",
            tax_year=2025,
            form_type="1099-INT",
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            normalized_decimal="120.11",
            raw_value="120.11",
            confidence=0.90,
            entity_tokens=["ally"],
        ),
        _base_row(
            record_id="i2",
            source="/Users/x/Tax/2025/TaxStatement_2025_1099INT_2.pdf",
            tax_year=2025,
            form_type="1099-INT",
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            normalized_decimal="34.89",
            raw_value="34.89",
            confidence=0.88,
            entity_tokens=["vanguard"],
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much interest did i earn in 2025",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "Interest income (2025): 155.00" in out["response"]


def test_tax_resolver_1099_threshold_does_not_require_year(monkeypatch):
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: [],
    )
    out = run_tax_resolver(
        query="what is the minimum to file form 1099-div",
        intent="TAX_QUERY",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "1099-DIV reporting threshold: generally $10+" in out["response"]


def test_tax_resolver_ignores_1099_form_number_artifact(monkeypatch):
    rows = [
        _base_row(
            record_id="d1",
            source="/Users/x/Tax/2025/2025-CORE-4385-Consolidated-Form-1099.pdf",
            tax_year=2025,
            form_type="1099-DIV",
            field_code="f1099_div_box_1a_total_ordinary_dividends",
            field_label="Form 1099-DIV box 1a total ordinary dividends",
            normalized_decimal="1099.00",
            raw_value="1099",
            confidence=0.90,
        ),
        _base_row(
            record_id="d2",
            source="/Users/x/Tax/2025/2025-CORE-4385-Consolidated-Form-1099.pdf",
            tax_year=2025,
            form_type="1099-DIV",
            field_code="f1099_div_box_1a_total_ordinary_dividends",
            field_label="Form 1099-DIV box 1a total ordinary dividends",
            normalized_decimal="222.14",
            raw_value="222.14",
            confidence=0.90,
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="what were my dividends in 2025",
        intent="TAX_QUERY",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "Dividend income (2025): 222.14" in out["response"]


def test_tax_resolver_interest_filters_year_and_large_id_artifacts(monkeypatch):
    rows = [
        _base_row(
            record_id="i1",
            source="/Users/x/Tax/2025/ally1099.pdf",
            tax_year=2025,
            form_type="1099-INT",
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            normalized_decimal="2025.00",
            raw_value="2025",
            confidence=0.90,
        ),
        _base_row(
            record_id="i2",
            source="/Users/x/Tax/2025/ally1099.pdf",
            tax_year=2025,
            form_type="1099-INT",
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            normalized_decimal="2223182698.00",
            raw_value="2223182698",
            confidence=0.90,
        ),
        _base_row(
            record_id="i3",
            source="/Users/x/Tax/2025/vanguard_1099.pdf",
            tax_year=2025,
            form_type="1099-INT",
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            normalized_decimal="16.12",
            raw_value="16.12",
            confidence=0.90,
        ),
    ]
    monkeypatch.setattr(
        "query.tax_resolver.load_tax_ledger_rows",
        lambda db_path, silo=None, tax_year=None: rows,
    )
    out = run_tax_resolver(
        query="how much interest did i earn in 2025",
        intent="MONEY_YEAR_TOTAL",
        db_path="/tmp/db",
        use_unified=True,
        silo="tax",
        source_label="Tax Professional",
        no_color=True,
    )
    assert out is not None
    assert out["guardrail_no_match"] is False
    assert "Interest income (2025): 16.12" in out["response"]
