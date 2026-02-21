"""Tier B extractor: layout-text patterns for machine-generated PDFs."""
from __future__ import annotations

import re
from typing import Any

from tax.normalize import normalize_money_value
from tax.schema import (
    F1040_LINE_CODES,
    F1099_DIV_CODES,
    F1099_INT_CODES,
    W2_BOX_FIELD_CODES,
)


def _append_match(
    out: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    *,
    field_code: str,
    field_label: str,
    form_type: str,
    value: str,
    confidence: float,
    tier: str = "layout",
) -> None:
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
            "confidence": confidence,
            "extractor_tier": tier,
        }
    )


def extract_layout_fields(
    text: str,
    form_type_hint: str | None = None,
    *,
    tier: str = "layout",
    base_confidence: float = 0.90,
) -> list[dict[str, Any]]:
    """Extract label-near-value pairs from flattened page text."""
    body = text or ""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    if form_type_hint in (None, "1040"):
        for line_num, (field_code, field_label) in F1040_LINE_CODES.items():
            label_first = rf"(?im)\bline\s*{line_num}\b[^\n]{{0,64}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+line\s*{line_num}\b"
            for m in re.finditer(label_first, body):
                _append_match(
                    out,
                    seen,
                    field_code=field_code,
                    field_label=field_label,
                    form_type="1040",
                    value=m.group(1),
                    confidence=base_confidence,
                    tier=tier,
                )
            for m in re.finditer(value_first, body):
                _append_match(
                    out,
                    seen,
                    field_code=field_code,
                    field_label=field_label,
                    form_type="1040",
                    value=m.group(1),
                    confidence=base_confidence - 0.04,
                    tier=tier,
                )

    if form_type_hint in (None, "W2", "W-2"):
        w2_patterns: dict[int, tuple[str, str, str]] = {
            1: (r"wages?\s*,?\s*tips?\s*,?\s*other\s+comp(?:ensation|\.)?", "W-2 Box 1 wages, tips, other compensation", "W2"),
            2: (r"federal\s+income\s+tax\s+withheld", "W-2 Box 2 federal income tax withheld", "W2"),
            3: (r"social\s+security\s+wages", "W-2 Box 3 social security wages", "W2"),
            4: (r"social\s+security\s+tax\s+withheld", "W-2 Box 4 social security tax withheld", "W2"),
            5: (r"medicare\s+wages(?:\s+and\s+tips)?", "W-2 Box 5 medicare wages and tips", "W2"),
            6: (r"medicare\s+tax\s+withheld", "W-2 Box 6 medicare tax withheld", "W2"),
            16: (r"state\s+wages", "W-2 Box 16 state wages, tips, etc.", "W2"),
            17: (r"state\s+income\s+tax", "W-2 Box 17 state income tax", "W2"),
            18: (r"local\s+wages", "W-2 Box 18 local wages, tips, etc.", "W2"),
            19: (r"local\s+income\s+tax", "W-2 Box 19 local income tax", "W2"),
        }
        for box_num, (label_pat, _label, form_type) in w2_patterns.items():
            canonical = W2_BOX_FIELD_CODES.get(box_num)
            if canonical is None:
                continue
            field_code, field_label = canonical
            label_first = rf"(?im){label_pat}[^\n]{{0,96}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+{label_pat}"
            number_label_first = rf"(?im)\b{box_num}\s+{label_pat}[^\n]{{0,64}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            number_value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+{box_num}\s+{label_pat}"
            box_label_first = rf"(?im)\bbox\s*{box_num}\b(?:\s*of\s*w-?2)?[^\n]{{0,64}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            for pat, conf in (
                (label_first, base_confidence),
                (value_first, base_confidence - 0.05),
                (number_label_first, base_confidence),
                (number_value_first, base_confidence - 0.05),
                (box_label_first, base_confidence),
            ):
                for m in re.finditer(pat, body):
                    _append_match(
                        out,
                        seen,
                        field_code=field_code,
                        field_label=field_label,
                        form_type=form_type,
                        value=m.group(1),
                        confidence=conf,
                        tier=tier,
                    )

    if form_type_hint in (None, "1099", "1099-INT"):
        for box, (field_code, field_label) in F1099_INT_CODES.items():
            box_pat = re.escape(box)
            label_first = rf"(?im)\bbox\s*{box_pat}\b[^\n]{{0,80}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            for m in re.finditer(label_first, body):
                _append_match(
                    out,
                    seen,
                    field_code=field_code,
                    field_label=field_label,
                    form_type="1099-INT",
                    value=m.group(1),
                    confidence=base_confidence - 0.02,
                    tier=tier,
                )

    if form_type_hint in (None, "1099", "1099-DIV"):
        label_map = {
            "1a": r"1a\s+total\s+ordinary\s+dividends",
            "2a": r"2a\s+total\s+capital\s+gain",
        }
        for box, (field_code, field_label) in F1099_DIV_CODES.items():
            pat_label = label_map.get(box, re.escape(box))
            label_first = rf"(?im){pat_label}[^\n]{{0,80}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+{pat_label}"
            for pat, conf in ((label_first, base_confidence - 0.02), (value_first, base_confidence - 0.06)):
                for m in re.finditer(pat, body):
                    _append_match(
                        out,
                        seen,
                        field_code=field_code,
                        field_label=field_label,
                        form_type="1099-DIV",
                        value=m.group(1),
                        confidence=conf,
                        tier=tier,
                    )

    return out
