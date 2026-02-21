from tax.extractors.layout import extract_layout_fields


def test_extract_layout_fields_w2_value_before_label():
    text = "4723.31 2 Federal Income tax withheld"
    rows = extract_layout_fields(text, form_type_hint="W2")
    assert any(r["field_code"] == "w2_box_2_federal_income_tax_withheld" for r in rows)


def test_extract_layout_fields_1040_label_before_value():
    text = "Form 1040 line 24 total tax 5100.50"
    rows = extract_layout_fields(text, form_type_hint="1040")
    match = [r for r in rows if r["field_code"] == "f1040_line_24_total_tax"]
    assert match
    assert match[0]["normalized_decimal"] == "5100.50"


def test_extract_layout_fields_w2_ignores_box_number_artifact():
    text = "1 Wages, tips, other compensation"
    rows = extract_layout_fields(text, form_type_hint="W2")
    assert not any(r["field_code"] == "w2_box_1_wages" for r in rows)


def test_extract_layout_fields_w2_prefers_number_value_first_pattern():
    text = "35676.33 1 Wages, tips, other compensation 4723.31 2 Federal Income tax withheld"
    rows = extract_layout_fields(text, form_type_hint="W2")
    wages = [r for r in rows if r["field_code"] == "w2_box_1_wages"]
    withheld = [r for r in rows if r["field_code"] == "w2_box_2_federal_income_tax_withheld"]
    assert any(r["normalized_decimal"] == "35676.33" for r in wages)
    assert any(r["normalized_decimal"] == "4723.31" for r in withheld)


def test_extract_layout_fields_w2_pair_block_two_values():
    text = (
        "1 Wages, tips, other compensation\n"
        "2 Federal income tax withheld\n"
        "4270.90\n"
        "333.49\n"
        "3 Social security wages\n"
    )
    rows = extract_layout_fields(text, form_type_hint="W2")
    wages = [r for r in rows if r["field_code"] == "w2_box_1_wages"]
    withheld = [r for r in rows if r["field_code"] == "w2_box_2_federal_income_tax_withheld"]
    assert any(r["normalized_decimal"] == "4270.90" for r in wages)
    assert any(r["normalized_decimal"] == "333.49" for r in withheld)


def test_extract_layout_fields_w2_dominant_amount_fallback():
    text = (
        "15128.97\n1382.88\n15128.97\n938.00\n15128.97\n219.37\n"
        "15128.97\n497.00\n15128.97\n"
    )
    rows = extract_layout_fields(text, form_type_hint="W2")
    wages = [r for r in rows if r["field_code"] == "w2_box_1_wages"]
    assert any(r["normalized_decimal"] == "15128.97" for r in wages)


def test_extract_layout_fields_1099_int_interest_income_label_patterns():
    text = "Interest Income Received (Box 1) 145.62 Federal Income Tax Withheld (Box 4) 0.00"
    rows = extract_layout_fields(text, form_type_hint="1099-INT")
    interest = [r for r in rows if r["field_code"] == "f1099_int_box_1_interest_income"]
    assert any(r["normalized_decimal"] == "145.62" for r in interest)


def test_extract_layout_fields_1099_int_numbered_interest_line_prefers_amount():
    text = "1- Interest income (not included in line 3) 16.12 2- Early withdrawal penalty 0.00"
    rows = extract_layout_fields(text, form_type_hint="1099-INT")
    interest = [r for r in rows if r["field_code"] == "f1099_int_box_1_interest_income"]
    assert any(r["normalized_decimal"] == "16.12" for r in interest)
    assert not any(r["normalized_decimal"] == "3.00" for r in interest)


def test_extract_layout_fields_1099_int_skips_form_number_artifact():
    text = "2025 1099-INT Interest Income Received (Box 1) 1099"
    rows = extract_layout_fields(text, form_type_hint="1099-INT")
    assert not any(r["normalized_decimal"] == "1099.00" for r in rows)


def test_extract_layout_fields_1099_int_amount_table_multiline():
    text = "Amount\n1\nInterest Income\n$19.67\n4\nFederal Income Tax Withheld\n$0.00"
    rows = extract_layout_fields(text, form_type_hint="1099")
    interest = [r for r in rows if r["field_code"] == "f1099_int_box_1_interest_income"]
    assert any(r["normalized_decimal"] == "19.67" for r in interest)


def test_extract_layout_fields_1099_int_vertical_label_and_value_lists():
    text = (
        "Interest income\n"
        "Early withdrawal penalty\n"
        "Interest on U.S. Savings Bonds and Treasury obligations\n"
        "Federal income tax withheld\n"
        "1\n2\n3\n4\n"
        "19.54\n0.00\n0.00\n0.00\n"
    )
    rows = extract_layout_fields(text, form_type_hint="1099-INT")
    interest = [r for r in rows if r["field_code"] == "f1099_int_box_1_interest_income"]
    withheld = [r for r in rows if r["field_code"] == "f1099_int_box_4_federal_income_tax_withheld"]
    assert any(r["normalized_decimal"] == "19.54" for r in interest)
    assert any(r["normalized_decimal"] == "0.00" for r in withheld)


def test_extract_layout_fields_1099_int_dotted_line_items():
    text = (
        "1  Interest Income .............................................0.00\n"
        "4  Federal Income Tax Withheld .................................0.00\n"
    )
    rows = extract_layout_fields(text, form_type_hint="1099-INT")
    interest = [r for r in rows if r["field_code"] == "f1099_int_box_1_interest_income"]
    withheld = [r for r in rows if r["field_code"] == "f1099_int_box_4_federal_income_tax_withheld"]
    assert any(r["normalized_decimal"] == "0.00" for r in interest)
    assert any(r["normalized_decimal"] == "0.00" for r in withheld)
