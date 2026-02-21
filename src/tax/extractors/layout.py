"""Tier B extractor: layout-text patterns for machine-generated PDFs."""
from __future__ import annotations

import re
from typing import Any

from tax.normalize import normalize_money_value, parse_decimal
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


def _extract_w2_pair_block_values(
    body: str,
    *,
    out: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    base_confidence: float,
    tier: str,
) -> None:
    """Extract common W-2 two-value blocks:
    box1+box2, box3+box4, box5+box6.
    """
    pair_specs = [
        (1, r"wages?\s*,?\s*tips?\s*,?\s*other\s+comp(?:ensation|\.)?", 2, r"federal\s+income\s+tax\s+withheld", r"\b3\s+"),
        (3, r"social\s+security\s+wages", 4, r"social\s+security\s+tax\s+withheld", r"\b5\s+"),
        (5, r"medicare\s+wages(?:\s+and\s+tips)?", 6, r"medicare\s+tax\s+withheld", r"\b7\s+|\bc\s+employer|$"),
    ]
    for left_box, left_pat, right_box, right_pat, stop_pat in pair_specs:
        left_meta = W2_BOX_FIELD_CODES.get(left_box)
        right_meta = W2_BOX_FIELD_CODES.get(right_box)
        if left_meta is None or right_meta is None:
            continue
        left_code, left_label = left_meta
        right_code, right_label = right_meta
        pat = (
            rf"(?is)\b{left_box}\s+{left_pat}\b.*?"
            rf"\b{right_box}\s+{right_pat}\b\s*"
            rf"([0-9][0-9,]*(?:\.\d{{1,2}})?)\s*"
            rf"([0-9][0-9,]*(?:\.\d{{1,2}})?)\s*"
            rf"(?={stop_pat})"
        )
        for m in re.finditer(pat, body):
            _append_match(
                out,
                seen,
                field_code=left_code,
                field_label=left_label,
                form_type="W2",
                value=m.group(1),
                confidence=min(0.99, base_confidence + 0.04),
                tier=tier,
            )
            _append_match(
                out,
                seen,
                field_code=right_code,
                field_label=right_label,
                form_type="W2",
                value=m.group(2),
                confidence=min(0.99, base_confidence + 0.04),
                tier=tier,
            )


def _extract_w2_box1_dominant_amount_fallback(
    body: str,
    *,
    out: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    base_confidence: float,
    tier: str,
) -> None:
    """Fallback for unlabeled W-2 text blocks: choose dominant repeated monetary amount."""
    box1_meta = W2_BOX_FIELD_CODES.get(1)
    if box1_meta is None:
        return
    box1_code, box1_label = box1_meta
    existing_box1 = [r for r in out if str(r.get("field_code") or "") == box1_code]
    if existing_box1:
        return
    raw_nums = re.findall(r"(?<!\d)([0-9][0-9,]{2,}(?:\.\d{2}))", body or "")
    if not raw_nums:
        return
    by_norm: dict[str, int] = {}
    for raw in raw_nums:
        norm = normalize_money_value(raw)
        if not norm:
            continue
        dec = parse_decimal(norm)
        if dec is None:
            continue
        if dec == dec.to_integral_value() and 1900 <= int(dec) <= 2099:
            continue
        if dec <= 100:
            continue
        by_norm[norm] = by_norm.get(norm, 0) + 1
    if not by_norm:
        return
    ranked: list[tuple[int, Any, str]] = []
    for norm, count in by_norm.items():
        dec = parse_decimal(norm)
        if dec is None:
            continue
        ranked.append((count, dec, norm))
    if not ranked:
        return
    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    best_count, _best_dec, best_norm = ranked[0]
    if best_count < 2:
        return
    _append_match(
        out,
        seen,
        field_code=box1_code,
        field_label=box1_label,
        form_type="W2",
        value=best_norm,
        confidence=max(0.60, base_confidence - 0.18),
        tier=tier,
    )


def _normalize_label_line(line: str) -> str:
    s = (line or "").strip().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def _is_money_line(line: str) -> bool:
    return bool(re.fullmatch(r"\$?\s*[0-9][0-9,]*(?:\.\d{2})\s*", line or ""))


def _is_small_int_line(line: str) -> bool:
    return bool(re.fullmatch(r"\s*\d{1,2}\s*", line or ""))


def _extract_1099_int_vertical_list_values(
    body: str,
    *,
    out: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    base_confidence: float,
    tier: str,
) -> None:
    """Extract 1099-INT values from vertical label list + vertical amount list layouts."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    if not lines:
        return
    norm_lines = [_normalize_label_line(ln) for ln in lines]
    target_interest = "interest income"
    target_withheld = "federal income tax withheld"
    for i, norm in enumerate(norm_lines):
        if norm != target_interest:
            continue
        labels: list[str] = []
        j = i
        while j < len(lines):
            cur = lines[j]
            cur_norm = norm_lines[j]
            if not cur_norm:
                break
            if _is_money_line(cur) or _is_small_int_line(cur):
                break
            if cur_norm.startswith("total 1099 int"):
                break
            labels.append(cur_norm)
            j += 1
            if len(labels) > 30:
                break
        if not labels or target_interest not in labels or target_withheld not in labels:
            continue
        k = j
        while k < len(lines) and _is_small_int_line(lines[k]):
            k += 1
        values: list[str] = []
        while k < len(lines) and _is_money_line(lines[k]):
            norm_val = normalize_money_value(lines[k])
            if norm_val:
                values.append(norm_val)
            k += 1
            if len(values) > 40:
                break
        interest_pos = labels.index(target_interest)
        withheld_pos = labels.index(target_withheld)
        if len(values) <= max(interest_pos, withheld_pos):
            continue
        _append_match(
            out,
            seen,
            field_code="f1099_int_box_1_interest_income",
            field_label="Form 1099-INT box 1 interest income",
            form_type="1099-INT",
            value=values[interest_pos],
            confidence=base_confidence - 0.03,
            tier=tier,
        )
        _append_match(
            out,
            seen,
            field_code="f1099_int_box_4_federal_income_tax_withheld",
            field_label="Form 1099-INT box 4 federal income tax withheld",
            form_type="1099-INT",
            value=values[withheld_pos],
            confidence=base_confidence - 0.03,
            tier=tier,
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
        _extract_w2_pair_block_values(
            body,
            out=out,
            seen=seen,
            base_confidence=base_confidence,
            tier=tier,
        )
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
            value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+{label_pat}"
            number_value_first = rf"(?im)\b([0-9][0-9,]*(?:\.\d{{1,2}})?)\s+{box_num}\s+{label_pat}"
            box_label_first = rf"(?im)\bbox\s*{box_num}\b(?:\s*of\s*w-?2)?[^\n]{{0,64}}?\$?\s*([0-9][0-9,]*(?:\.\d{{1,2}})?)"
            for pat, conf in (
                (value_first, base_confidence - 0.05),
                (number_value_first, base_confidence - 0.05),
                (box_label_first, base_confidence),
            ):
                for m in re.finditer(pat, body):
                    norm = normalize_money_value(m.group(1))
                    if norm == f"{box_num:.2f}":
                        # Common flattened/OCR artifact: captured box number, not field value.
                        continue
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
        _extract_w2_box1_dominant_amount_fallback(
            body,
            out=out,
            seen=seen,
            base_confidence=base_confidence,
            tier=tier,
        )

    if form_type_hint in (None, "1099", "1099-INT"):
        money_pat = r"([0-9][0-9,]*(?:\.\d{2}))"
        near_pat = r"[\s\S]{0,120}?"
        _extract_1099_int_vertical_list_values(
            body,
            out=out,
            seen=seen,
            base_confidence=base_confidence,
            tier=tier,
        )
        label_map = {
            "1": r"(?:interest\s+income(?:\s+received)?)(?:\s*\(\s*box\s*1\s*\))?",
            "4": r"(?:federal\s+income\s+tax\s+withheld)(?:\s*\(\s*box\s*4\s*\))?",
        }
        for box, (field_code, field_label) in F1099_INT_CODES.items():
            box_pat = re.escape(box)
            pat_label = label_map.get(box)
            value_first = (
                rf"(?im)\b{money_pat}\s+{pat_label}"
                if pat_label
                else None
            )
            compact_label_value = (
                rf"(?im){pat_label}[^\n]{{0,20}}?\$?\s*{money_pat}"
                if pat_label
                else None
            )
            numbered_label = (
                rf"(?im)\b{box_pat}\s*[-:]\s*{pat_label}{near_pat}\$?\s*{money_pat}"
                if pat_label
                else None
            )
            amount_table = (
                rf"(?im)\bamount\s+{box_pat}\s+{pat_label}{near_pat}\$?\s*{money_pat}"
                if pat_label
                else None
            )
            dotted_line_item = (
                rf"(?im)\b{box_pat}\s+{pat_label}\s*[.\s]{{3,}}\$?\s*{money_pat}"
                if pat_label
                else None
            )
            candidates: list[tuple[str, float]] = []
            if compact_label_value:
                candidates.append((compact_label_value, base_confidence - 0.03))
            if value_first:
                candidates.append((value_first, base_confidence - 0.06))
            if numbered_label:
                candidates.append((numbered_label, base_confidence - 0.01))
            if amount_table:
                candidates.append((amount_table, base_confidence - 0.01))
            if dotted_line_item:
                candidates.append((dotted_line_item, base_confidence - 0.01))
            for pat, conf in candidates:
                for m in re.finditer(pat, body):
                    norm = normalize_money_value(m.group(1))
                    if not norm:
                        continue
                    if norm in {"1099.00", f"{int(float(box)):.2f}"}:
                        continue
                    _append_match(
                        out,
                        seen,
                        field_code=field_code,
                        field_label=field_label,
                        form_type="1099-INT",
                        value=m.group(1),
                        confidence=conf,
                        tier=tier,
                    )

    if form_type_hint in (None, "1099", "1099-DIV"):
        money_pat = r"([0-9][0-9,]*(?:\.\d{2}))"
        near_pat = r"[\s\S]{0,120}?"
        label_map = {
            "1a": r"1a\s+total\s+ordinary\s+dividends",
            "2a": r"2a\s+total\s+capital\s+gain",
        }
        for box, (field_code, field_label) in F1099_DIV_CODES.items():
            pat_label = label_map.get(box, re.escape(box))
            label_first = rf"(?im){pat_label}{near_pat}\$?\s*{money_pat}"
            value_first = rf"(?im)\b{money_pat}\s+{pat_label}"
            for pat, conf in ((label_first, base_confidence - 0.02), (value_first, base_confidence - 0.06)):
                for m in re.finditer(pat, body):
                    norm = normalize_money_value(m.group(1))
                    if norm == "1099.00":
                        continue
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
