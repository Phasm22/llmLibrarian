"""Deterministic tax resolver backed by ingest-time tax ledger rows."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import re
from typing import Any

from style import bold, dim, label_style
from query.formatting import format_source

from tax.ledger import load_tax_ledger_rows
from tax.normalize import format_money_decimal, parse_decimal, source_tokens
from tax.query_contract import (
    METRIC_AGI,
    METRIC_DIVIDENDS,
    METRIC_FEDERAL_WITHHELD,
    METRIC_INTEREST_INCOME,
    METRIC_1099_MIN_REPORTING_THRESHOLD,
    METRIC_PAYROLL_TAXES,
    METRIC_STATE_TAX,
    METRIC_TOTAL_INCOME,
    METRIC_TOTAL_TAX_LIABILITY,
    METRIC_W2_BOX,
    METRIC_WAGES,
    TaxQuery,
    parse_tax_query,
)
from tax.schema import W2_BOX_FIELD_CODES

_MIN_CONFIDENCE = 0.55


def run_tax_resolver(
    *,
    query: str,
    intent: str,
    db_path: str,
    use_unified: bool,
    silo: str | None,
    source_label: str,
    no_color: bool,
) -> dict[str, Any] | None:
    """Resolve tax asks from ledger rows only. Returns guardrail payload or None if not tax-domain."""
    request = parse_tax_query(query)
    if request is None:
        return None

    if request.metric == METRIC_1099_MIN_REPORTING_THRESHOLD:
        return _resolve_1099_min_reporting_threshold(request, source_label, no_color)

    if request.tax_year is None:
        return _abstain_response(
            source_label=source_label,
            no_color=no_color,
            category="ambiguous_scope",
            message="Include a tax year (for example: 2025).",
            metric=None,
            form_type=request.form_type_hint,
            tax_year=None,
            rows=[],
        )

    rows = load_tax_ledger_rows(
        db_path,
        silo=(silo if (use_unified and silo) else None),
        tax_year=request.tax_year,
    )
    if not rows:
        if intent != "TAX_QUERY":
            # Compatibility fallback for older indexes/unit tests that do not have ledger rows yet.
            return None
        return _abstain_response(
            source_label=source_label,
            no_color=no_color,
            category="no_match",
            message=f"No tax ledger rows found for {request.tax_year} in this scope.",
            metric=request.field_code_hint,
            form_type=request.form_type_hint,
            tax_year=request.tax_year,
            rows=[],
        )

    if request.metric == METRIC_TOTAL_INCOME:
        return _resolve_total_income(request, rows, source_label, no_color)
    if request.metric == METRIC_AGI:
        return _resolve_single_field_metric(
            request,
            rows,
            field_codes=["f1040_line_11_agi"],
            label="Adjusted gross income",
            source_label=source_label,
            no_color=no_color,
            form_type="1040",
        )
    if request.metric == METRIC_TOTAL_TAX_LIABILITY:
        return _resolve_single_field_metric(
            request,
            rows,
            field_codes=["f1040_line_24_total_tax"],
            label="Total tax liability",
            source_label=source_label,
            no_color=no_color,
            form_type="1040",
        )
    if request.metric == METRIC_FEDERAL_WITHHELD:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["w2_box_2_federal_income_tax_withheld"],
            label="Federal income tax withheld",
            source_label=source_label,
            no_color=no_color,
            form_type="W2",
            interpretation=request.interpretation,
        )
    if request.metric == METRIC_STATE_TAX:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["w2_box_17_state_income_tax"],
            label="State income tax withheld",
            source_label=source_label,
            no_color=no_color,
            form_type="W2",
        )
    if request.metric == METRIC_PAYROLL_TAXES:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["w2_box_4_social_security_tax_withheld", "w2_box_6_medicare_tax_withheld"],
            label="Payroll taxes withheld (SS + Medicare)",
            source_label=source_label,
            no_color=no_color,
            form_type="W2",
        )
    if request.metric == METRIC_WAGES:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["w2_box_1_wages"],
            label="W-2 wages",
            source_label=source_label,
            no_color=no_color,
            form_type="W2",
        )
    if request.metric == METRIC_INTEREST_INCOME:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["f1099_int_box_1_interest_income"],
            label="Interest income",
            source_label=source_label,
            no_color=no_color,
            form_type="1099-INT",
        )
    if request.metric == METRIC_DIVIDENDS:
        return _resolve_sum_metric(
            request,
            rows,
            field_codes=["f1099_div_box_1a_total_ordinary_dividends"],
            label="Dividend income",
            source_label=source_label,
            no_color=no_color,
            form_type="1099-DIV",
        )
    if request.metric == METRIC_W2_BOX:
        if request.box_number is None:
            return _abstain_response(
                source_label=source_label,
                no_color=no_color,
                category="ambiguous_scope",
                message="Specify a valid W-2 box number.",
                metric=request.field_code_hint,
                form_type="W2",
                tax_year=request.tax_year,
                rows=[],
            )
        box_meta = W2_BOX_FIELD_CODES.get(request.box_number)
        if box_meta is None:
            return _abstain_response(
                source_label=source_label,
                no_color=no_color,
                category="no_match",
                message=f"W-2 box {request.box_number} is not supported in v1.",
                metric=request.field_code_hint,
                form_type="W2",
                tax_year=request.tax_year,
                rows=[],
            )
        field_code, field_label = box_meta
        return _resolve_single_field_metric(
            request,
            rows,
            field_codes=[field_code],
            label=field_label,
            source_label=source_label,
            no_color=no_color,
            form_type="W2",
        )

    return _abstain_response(
        source_label=source_label,
        no_color=no_color,
        category="no_match",
        message="Could not map query to a supported deterministic tax metric.",
        metric=request.field_code_hint,
        form_type=request.form_type_hint,
        tax_year=request.tax_year,
        rows=[],
    )


def _resolve_total_income(
    request: TaxQuery,
    rows: list[dict[str, Any]],
    source_label: str,
    no_color: bool,
) -> dict[str, Any]:
    primary = _filter_rows(rows, request=request, field_codes=["f1040_line_9_total_income"])
    if primary["status"] == "ok":
        return _single_value_response(
            request=request,
            source_label=source_label,
            no_color=no_color,
            label="Total income",
            form_type="1040",
            field_code="f1040_line_9_total_income",
            rows=primary["rows"],
            interpretation=None,
        )
    if primary["status"] in {"conflict", "low_confidence_extraction"}:
        return _status_to_abstain(primary, request, source_label, no_color, "f1040_line_9_total_income", "1040")

    wages = _filter_rows(rows, request=request, field_codes=["w2_box_1_wages"])
    if wages["status"] != "ok":
        return _status_to_abstain(wages, request, source_label, no_color, "w2_box_1_wages", "W2")

    return _sum_value_response(
        request=request,
        source_label=source_label,
        no_color=no_color,
        label="Total income",
        form_type="W2",
        field_code="w2_box_1_wages",
        rows=wages["rows"],
        interpretation=None,
        fallback_prefix=None,
    )


def _resolve_1099_min_reporting_threshold(
    request: TaxQuery,
    source_label: str,
    no_color: bool,
) -> dict[str, Any]:
    form_hint = (request.form_type_hint or "").upper()
    if form_hint == "1099-INT":
        line = "1099-INT reporting threshold: generally $10+ of interest (or any amount with backup withholding)."
    elif form_hint == "1099-DIV":
        line = "1099-DIV reporting threshold: generally $10+ of dividends/distributions (or any amount with backup withholding)."
    else:
        line = "1099-INT/1099-DIV reporting threshold: generally $10+ (or any amount with backup withholding)."
    return _final_response(
        line=line,
        source_label=source_label,
        no_color=no_color,
        metric="1099_reporting_threshold",
        form_type=request.form_type_hint or "1099",
        tax_year=request.tax_year,
        rows=[],
        guardrail_no_match=False,
        guardrail_reason="tax_reference_threshold",
    )


def _resolve_single_field_metric(
    request: TaxQuery,
    rows: list[dict[str, Any]],
    *,
    field_codes: list[str],
    label: str,
    source_label: str,
    no_color: bool,
    form_type: str,
) -> dict[str, Any]:
    status = _filter_rows(rows, request=request, field_codes=field_codes)
    if status["status"] != "ok":
        return _status_to_abstain(status, request, source_label, no_color, field_codes[0], form_type)
    return _single_value_response(
        request=request,
        source_label=source_label,
        no_color=no_color,
        label=label,
        form_type=form_type,
        field_code=field_codes[0],
        rows=status["rows"],
        interpretation=request.interpretation,
    )


def _resolve_sum_metric(
    request: TaxQuery,
    rows: list[dict[str, Any]],
    *,
    field_codes: list[str],
    label: str,
    source_label: str,
    no_color: bool,
    form_type: str,
    interpretation: str | None = None,
) -> dict[str, Any]:
    status = _filter_rows(rows, request=request, field_codes=field_codes)
    if status["status"] != "ok":
        field_code = field_codes[0] if len(field_codes) == 1 else "+".join(field_codes)
        return _status_to_abstain(status, request, source_label, no_color, field_code, form_type)
    return _sum_value_response(
        request=request,
        source_label=source_label,
        no_color=no_color,
        label=label,
        form_type=form_type,
        field_code=(field_codes[0] if len(field_codes) == 1 else "+".join(field_codes)),
        rows=status["rows"],
        interpretation=interpretation,
    )


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    request: TaxQuery,
    field_codes: list[str],
) -> dict[str, Any]:
    candidates = [r for r in rows if str(r.get("field_code") or "") in set(field_codes)]
    if request.employer_tokens:
        candidates = [r for r in candidates if _row_matches_employer(r, request.employer_tokens)]
        if not candidates:
            return {"status": "no_match", "message": f"No rows matched employer '{request.employer or ''}'.", "rows": []}

    if not candidates:
        return {"status": "no_match", "message": "No matching field rows found.", "rows": []}

    high_conf = [r for r in candidates if float(r.get("confidence") or 0.0) >= _MIN_CONFIDENCE]
    if not high_conf:
        return {"status": "low_confidence_extraction", "message": "Extraction confidence was too low.", "rows": candidates}

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in high_conf:
        grouped[(str(row.get("source") or ""), int(row.get("page") or 1), str(row.get("field_code") or ""))].append(row)

    resolved: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for (source, page, field_code), items in grouped.items():
        items = [
            i
            for i in items
            if not _looks_like_w2_label_number_artifact(field_code, str(i.get("normalized_decimal") or ""))
            and not _looks_like_w2_year_artifact(field_code, str(i.get("normalized_decimal") or ""))
            and not _looks_like_1040_line_artifact(field_code, str(i.get("normalized_decimal") or ""))
            and not _looks_like_1099_form_number_artifact(field_code, str(i.get("normalized_decimal") or ""))
        ]
        if not items:
            continue
        values = {str(i.get("normalized_decimal") or "") for i in items if str(i.get("normalized_decimal") or "")}
        if len(values) > 1:
            if field_code == "w2_box_1_wages":
                chosen = _choose_w2_box1_candidate(items)
                if chosen is not None:
                    resolved.append(chosen)
                    continue
            if field_code == "w2_box_2_federal_income_tax_withheld":
                chosen = _choose_w2_box2_candidate(
                    items,
                    all_rows=rows,
                    source=str(source),
                    page=int(page),
                )
                if chosen is not None:
                    resolved.append(chosen)
                    continue
            conflicts.extend(items)
            continue
        chosen = sorted(items, key=lambda r: float(r.get("confidence") or 0.0), reverse=True)[0]
        resolved.append(chosen)

    if conflicts:
        return {"status": "conflict", "message": "Conflicting extracted values found.", "rows": conflicts}

    if not resolved:
        return {"status": "no_match", "message": "No resolved value rows.", "rows": []}

    return {"status": "ok", "rows": resolved}


def _looks_like_w2_label_number_artifact(field_code: str, normalized_decimal: str) -> bool:
    if not field_code.startswith("w2_box_"):
        return False
    dec = parse_decimal(normalized_decimal)
    if dec is None:
        return False
    if dec != dec.to_integral_value():
        return False
    value = int(dec)
    if 0 <= value <= 20:
        return True
    box_m = re.match(r"w2_box_(\d+)_", field_code)
    if box_m and value == int(box_m.group(1)):
        return True
    return False


def _looks_like_1040_line_artifact(field_code: str, normalized_decimal: str) -> bool:
    if not field_code.startswith("f1040_line_"):
        return False
    dec = parse_decimal(normalized_decimal)
    if dec is None:
        return False
    if dec != dec.to_integral_value():
        return False
    value = int(dec)
    if value <= 0:
        return False
    line_m = re.match(r"f1040_line_(\d+)_", field_code)
    if line_m:
        line_num = int(line_m.group(1))
        # Common worksheet artifacts are tiny integers around the referenced line label.
        if value <= 50 and abs(value - line_num) <= 3:
            return True
    # "Form 1040" and related header tokens often leak as numeric artifacts.
    if value in {104, 1040}:
        return True
    # Income/tax lines should not resolve to tiny worksheet marker values.
    if field_code in {"f1040_line_9_total_income", "f1040_line_11_agi", "f1040_line_24_total_tax"} and value <= 50:
        return True
    return False


def _looks_like_w2_year_artifact(field_code: str, normalized_decimal: str) -> bool:
    if field_code != "w2_box_1_wages":
        return False
    dec = parse_decimal(normalized_decimal)
    if dec is None or dec != dec.to_integral_value():
        return False
    year_like = int(dec)
    return 1900 <= year_like <= 2099


def _looks_like_1099_form_number_artifact(field_code: str, normalized_decimal: str) -> bool:
    if field_code not in {
        "f1099_int_box_1_interest_income",
        "f1099_int_box_4_federal_income_tax_withheld",
        "f1099_div_box_1a_total_ordinary_dividends",
        "f1099_div_box_2a_total_capital_gain",
    }:
        return False
    dec = parse_decimal(normalized_decimal)
    if dec is None or dec != dec.to_integral_value():
        return False
    if int(dec) == 1099:
        return True
    if field_code == "f1099_int_box_1_interest_income":
        value = int(dec)
        if 1900 <= value <= 2099:
            return True
        if value in {1, 3, 4}:
            return True
    if dec >= Decimal("1000000"):
        return True
    return False


def _choose_w2_box1_candidate(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[Decimal, float, dict[str, Any]]] = []
    for row in items:
        dec = parse_decimal(str(row.get("normalized_decimal") or ""))
        if dec is None or dec <= 0:
            continue
        if dec == dec.to_integral_value() and 1900 <= int(dec) <= 2099:
            # Ignore likely year artifacts (e.g., 2022 from worksheet/header text).
            continue
        ranked.append((dec, float(row.get("confidence") or 0.0), row))
    if not ranked:
        return None
    # Box 1 is wages; in noisy OCR/layout duplicates, keep the largest plausible amount deterministically.
    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return ranked[0][2]


def _choose_w2_box2_candidate(
    items: list[dict[str, Any]],
    *,
    all_rows: list[dict[str, Any]],
    source: str,
    page: int,
) -> dict[str, Any] | None:
    ranked: list[tuple[Decimal, float, dict[str, Any]]] = []
    for row in items:
        dec = parse_decimal(str(row.get("normalized_decimal") or ""))
        if dec is None or dec <= 0:
            continue
        ranked.append((dec, float(row.get("confidence") or 0.0), row))
    if not ranked:
        return None

    box1_amount = _choose_w2_box1_amount_for_source_page(all_rows, source=source, page=page)
    if box1_amount is not None:
        under_wages = [t for t in ranked if t[0] < box1_amount]
        if under_wages:
            ranked = under_wages

    ranked.sort(key=lambda t: (t[1], t[0]), reverse=True)
    return ranked[0][2]


def _choose_w2_box1_amount_for_source_page(
    all_rows: list[dict[str, Any]],
    *,
    source: str,
    page: int,
) -> Decimal | None:
    candidates = [
        r
        for r in all_rows
        if str(r.get("source") or "") == source
        and int(r.get("page") or 1) == page
        and str(r.get("field_code") or "") == "w2_box_1_wages"
        and float(r.get("confidence") or 0.0) >= _MIN_CONFIDENCE
    ]
    candidates = [
        r
        for r in candidates
        if not _looks_like_w2_label_number_artifact("w2_box_1_wages", str(r.get("normalized_decimal") or ""))
    ]
    if not candidates:
        return None
    chosen = _choose_w2_box1_candidate(candidates)
    if chosen is None:
        return None
    return parse_decimal(str(chosen.get("normalized_decimal") or ""))


def _row_matches_employer(row: dict[str, Any], employer_tokens: tuple[str, ...]) -> bool:
    row_tokens = {str(t).lower() for t in (row.get("entity_tokens") or [])}
    if employer_tokens and set(employer_tokens).issubset(row_tokens):
        return True
    src_tokens = set(source_tokens(str(row.get("source") or "")))
    return bool(employer_tokens and set(employer_tokens).issubset(src_tokens))


def _single_value_response(
    *,
    request: TaxQuery,
    source_label: str,
    no_color: bool,
    label: str,
    form_type: str,
    field_code: str,
    rows: list[dict[str, Any]],
    interpretation: str | None = None,
) -> dict[str, Any]:
    values = sorted({str(r.get("normalized_decimal") or "") for r in rows if str(r.get("normalized_decimal") or "")})
    if len(values) != 1:
        detail = _format_conflict_candidates(rows)
        msg = "Conflicting values found for requested field."
        if detail:
            msg = f"{msg} Candidates: {detail}"
        return _abstain_response(
            source_label=source_label,
            no_color=no_color,
            category="conflict",
            message=msg,
            metric=field_code,
            form_type=form_type,
            tax_year=request.tax_year,
            rows=rows,
        )

    value = format_money_decimal(values[0])
    employer_segment = f" at {request.employer}" if request.employer else ""
    line = f"{label}{employer_segment} ({request.tax_year}): {value}"
    if interpretation:
        line = f"{line} [{interpretation}]"

    return _final_response(
        line=line,
        source_label=source_label,
        no_color=no_color,
        metric=field_code,
        form_type=form_type,
        tax_year=request.tax_year,
        rows=rows,
        guardrail_no_match=False,
        guardrail_reason="tax_resolved",
    )


def _sum_value_response(
    *,
    request: TaxQuery,
    source_label: str,
    no_color: bool,
    label: str,
    form_type: str,
    field_code: str,
    rows: list[dict[str, Any]],
    interpretation: str | None = None,
    fallback_prefix: str | None = None,
) -> dict[str, Any]:
    total = Decimal("0")
    used_rows: list[dict[str, Any]] = []
    for row in rows:
        dec = parse_decimal(str(row.get("normalized_decimal") or ""))
        if dec is None:
            continue
        total += dec
        used_rows.append(row)

    if not used_rows:
        return _abstain_response(
            source_label=source_label,
            no_color=no_color,
            category="no_match",
            message="No numeric values were extractable.",
            metric=field_code,
            form_type=form_type,
            tax_year=request.tax_year,
            rows=rows,
        )

    employer_segment = f" at {request.employer}" if request.employer else ""
    prefix = f" {fallback_prefix}" if fallback_prefix else ""
    line = f"{label}{employer_segment} ({request.tax_year}){prefix}: {format_money_decimal(str(total))}"
    if interpretation:
        line = f"{line} [{interpretation}]"

    return _final_response(
        line=line,
        source_label=source_label,
        no_color=no_color,
        metric=field_code,
        form_type=form_type,
        tax_year=request.tax_year,
        rows=used_rows,
        guardrail_no_match=False,
        guardrail_reason="tax_resolved",
    )


def _status_to_abstain(
    status: dict[str, Any],
    request: TaxQuery,
    source_label: str,
    no_color: bool,
    metric: str,
    form_type: str,
) -> dict[str, Any]:
    category = str(status.get("status") or "no_match")
    message = str(status.get("message") or "No matching rows found.")
    if category == "conflict":
        detail = _format_conflict_candidates(status.get("rows") or [])
        if detail:
            message = f"{message} Candidates: {detail}"
    if category == "ok":
        category = "no_match"
    return _abstain_response(
        source_label=source_label,
        no_color=no_color,
        category=category,
        message=message,
        metric=metric,
        form_type=form_type,
        tax_year=request.tax_year,
        rows=status.get("rows") or [],
    )


def _abstain_response(
    *,
    source_label: str,
    no_color: bool,
    category: str,
    message: str,
    metric: str | None,
    form_type: str | None,
    tax_year: int | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    line = f"Abstain [{category}]: {message}"
    return _final_response(
        line=line,
        source_label=source_label,
        no_color=no_color,
        metric=metric,
        form_type=form_type,
        tax_year=tax_year,
        rows=rows,
        guardrail_no_match=True,
        guardrail_reason=f"tax_{category}",
    )


def _final_response(
    *,
    line: str,
    source_label: str,
    no_color: bool,
    metric: str | None,
    form_type: str | None,
    tax_year: int | None,
    rows: list[dict[str, Any]],
    guardrail_no_match: bool,
    guardrail_reason: str,
) -> dict[str, Any]:
    out = [line, "", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")]
    metric_str = metric or "n/a"
    form_str = form_type or "n/a"
    year_str = str(tax_year) if tax_year is not None else "n/a"
    source_metas = _compact_source_metas(rows)
    if source_metas:
        out.extend(["", bold(no_color, "Sources:")])
        for meta in source_metas:
            out.append(format_source("", meta, None, include_snippet=False, no_color=no_color))

    receipt_metas = [
        {
            "source": str(r.get("source") or ""),
            "page": int(r.get("page") or 1),
            "field_code": str(r.get("field_code") or ""),
        }
        for r in rows[:8]
    ]

    return {
        "response": "\n".join(out),
        "guardrail_no_match": guardrail_no_match,
        "guardrail_reason": guardrail_reason,
        "requested_year": year_str if year_str != "n/a" else None,
        "requested_form": form_str if form_str != "n/a" else None,
        "requested_line": metric_str if metric_str != "n/a" else None,
        "num_docs": len(rows),
        "receipt_metas": receipt_metas,
    }


def _compact_source_metas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("source") or "")
        if not source:
            continue
        page = int(row.get("page") or 1)
        key = (source, page)
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": source, "page": page})
    return out


def _format_conflict_candidates(rows: list[dict[str, Any]], *, max_items: int = 4) -> str:
    seen: set[tuple[str, str, int]] = set()
    parts: list[str] = []
    for row in rows:
        value = str(row.get("normalized_decimal") or "")
        source = str(row.get("source") or "")
        page = int(row.get("page") or 1)
        if not value or not source:
            continue
        key = (value, source, page)
        if key in seen:
            continue
        seen.add(key)
        name = source.rsplit("/", 1)[-1]
        parts.append(f"{value} ({name} p{page})")
        if len(parts) >= max_items:
            break
    return "; ".join(parts)
