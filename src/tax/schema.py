"""Tax ledger schema constants and shared mappings."""
from __future__ import annotations

from typing import TypedDict, Literal

ExtractorTier = Literal["form_field", "layout", "ocr_layout"]


class TaxLedgerRow(TypedDict):
    record_id: str
    silo: str
    source: str
    page: int
    doc_hash: str
    tax_year: int
    form_type: str
    field_code: str
    field_label: str
    entity_name: str
    entity_tokens: list[str]
    raw_value: str
    normalized_decimal: str
    currency: str
    extractor_tier: ExtractorTier
    confidence: float
    trace_ref: str
    created_at: str


FORM_W2 = "W2"
FORM_1040 = "1040"
FORM_1099_INT = "1099-INT"
FORM_1099_DIV = "1099-DIV"
FORM_1099_B = "1099-B"

W2_BOX_FIELD_CODES: dict[int, tuple[str, str]] = {
    1: ("w2_box_1_wages", "W-2 Box 1 wages, tips, other compensation"),
    2: ("w2_box_2_federal_income_tax_withheld", "W-2 Box 2 federal income tax withheld"),
    3: ("w2_box_3_social_security_wages", "W-2 Box 3 social security wages"),
    4: ("w2_box_4_social_security_tax_withheld", "W-2 Box 4 social security tax withheld"),
    5: ("w2_box_5_medicare_wages", "W-2 Box 5 medicare wages and tips"),
    6: ("w2_box_6_medicare_tax_withheld", "W-2 Box 6 medicare tax withheld"),
    16: ("w2_box_16_state_wages", "W-2 Box 16 state wages, tips, etc."),
    17: ("w2_box_17_state_income_tax", "W-2 Box 17 state income tax"),
    18: ("w2_box_18_local_wages", "W-2 Box 18 local wages, tips, etc."),
    19: ("w2_box_19_local_income_tax", "W-2 Box 19 local income tax"),
}

F1040_LINE_CODES: dict[int, tuple[str, str]] = {
    9: ("f1040_line_9_total_income", "Form 1040 line 9 total income"),
    11: ("f1040_line_11_agi", "Form 1040 line 11 adjusted gross income"),
    16: ("f1040_line_16_tax", "Form 1040 line 16 tax"),
    24: ("f1040_line_24_total_tax", "Form 1040 line 24 total tax"),
}

F1099_INT_CODES: dict[str, tuple[str, str]] = {
    "1": ("f1099_int_box_1_interest_income", "Form 1099-INT box 1 interest income"),
    "4": ("f1099_int_box_4_federal_income_tax_withheld", "Form 1099-INT box 4 federal income tax withheld"),
}

F1099_DIV_CODES: dict[str, tuple[str, str]] = {
    "1a": ("f1099_div_box_1a_total_ordinary_dividends", "Form 1099-DIV box 1a total ordinary dividends"),
    "2a": ("f1099_div_box_2a_total_capital_gain", "Form 1099-DIV box 2a total capital gain distributions"),
}

F1099_B_CODES: dict[str, tuple[str, str]] = {
    "totals": ("f1099_b_totals", "Form 1099-B totals summary"),
}

FIELD_CODE_TO_FORM_TYPE: dict[str, str] = {
    **{code: FORM_W2 for code, _label in W2_BOX_FIELD_CODES.values()},
    **{code: FORM_1040 for code, _label in F1040_LINE_CODES.values()},
    **{code: FORM_1099_INT for code, _label in F1099_INT_CODES.values()},
    **{code: FORM_1099_DIV for code, _label in F1099_DIV_CODES.values()},
    **{code: FORM_1099_B for code, _label in F1099_B_CODES.values()},
}
