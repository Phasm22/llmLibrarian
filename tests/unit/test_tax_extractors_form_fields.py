from tax.extractors.form_fields import extract_form_fields


def test_extract_form_fields_w2_boxes():
    text = "Form W-2\nBox 1 of W-2: 4,626.76\nBox 2: 4723.31"
    rows = extract_form_fields(text, form_type_hint="W2")
    codes = {r["field_code"] for r in rows}
    assert "w2_box_1_wages" in codes
    assert "w2_box_2_federal_income_tax_withheld" in codes


def test_extract_form_fields_1040_lines():
    text = "Form 1040\nline 9: 40303.09\nline 11: 38900.00"
    rows = extract_form_fields(text, form_type_hint="1040")
    vals = {r["field_code"]: r["normalized_decimal"] for r in rows}
    assert vals["f1040_line_9_total_income"] == "40303.09"
    assert vals["f1040_line_11_agi"] == "38900.00"
