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
