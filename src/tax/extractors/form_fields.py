"""Tier A extractor: direct form-field style labels (highest precision)."""
from __future__ import annotations

import re
from typing import Any

from tax.normalize import normalize_money_value
from tax.schema import F1040_LINE_CODES, W2_BOX_FIELD_CODES


def extract_form_fields(
    text: str,
    form_type_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Extract high-confidence labeled values such as 'line 9: 7,522' or 'Box 2: 4,723.31'."""
    body = text or ""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(field_code: str, field_label: str, value: str, form_type: str) -> None:
        norm = normalize_money_value(value)
        if not norm:
            return
        key = (field_code, norm)
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "form_type": form_type,
                "field_code": field_code,
                "field_label": field_label,
                "raw_value": value.strip(),
                "normalized_decimal": norm,
                "confidence": 0.99,
                "extractor_tier": "form_field",
            }
        )

    if form_type_hint in (None, "1040"):
        for line_num, (field_code, field_label) in F1040_LINE_CODES.items():
            pat = rf"(?im)\bline\s*{line_num}\s*[:\-]\s*\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            for m in re.finditer(pat, body):
                _add(field_code, field_label, m.group(1), "1040")

    if form_type_hint in (None, "W2", "W-2"):
        for box_num, (field_code, field_label) in W2_BOX_FIELD_CODES.items():
            pat = rf"(?im)\bbox\s*{box_num}\b(?:\s*of\s*w-?2)?\s*[:\-]?\s*\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            for m in re.finditer(pat, body):
                _add(field_code, field_label, m.group(1), "W2")

    return out
