from tax.extractors.ocr_layout import extract_ocr_layout_fields


def test_extract_ocr_layout_fields_handles_basic_w2_text():
    text = "OCR text (scan fallback):\n4,723.31 2 Federal Income tax withheld"
    rows = extract_ocr_layout_fields(text, form_type_hint="W2")
    assert any(r["field_code"] == "w2_box_2_federal_income_tax_withheld" for r in rows)


def test_extract_ocr_layout_fields_uses_ocr_tier():
    text = "OCR text (scan fallback):\nBox 1 of W-2: 4,626.76"
    rows = extract_ocr_layout_fields(text, form_type_hint="W2")
    assert rows
    assert all(r["extractor_tier"] == "ocr_layout" for r in rows)
