"""Structured tax query contract and deterministic parser."""
from __future__ import annotations

from dataclasses import dataclass
import re


METRIC_TOTAL_INCOME = "total_income"
METRIC_AGI = "agi"
METRIC_TOTAL_TAX_LIABILITY = "total_tax_liability"
METRIC_FEDERAL_WITHHELD = "federal_income_tax_withheld"
METRIC_PAYROLL_TAXES = "payroll_taxes"
METRIC_STATE_TAX = "state_income_tax"
METRIC_WAGES = "wages"
METRIC_W2_BOX = "w2_box"

_EMPLOYER_STOPWORDS = {
    "the",
    "and",
    "for",
    "from",
    "at",
    "in",
    "on",
    "my",
    "me",
    "i",
    "did",
    "do",
    "does",
    "was",
    "were",
    "to",
    "of",
    "taxes",
    "tax",
}


@dataclass(frozen=True)
class TaxQuery:
    raw_query: str
    tax_year: int | None
    metric: str
    form_type_hint: str | None
    field_code_hint: str | None
    employer: str | None
    employer_tokens: tuple[str, ...]
    box_number: int | None
    interpretation: str | None


def _parse_year(query: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", query)
    if not m:
        return None
    return int(m.group(1))


def _parse_employer(query: str, year: int | None) -> tuple[str | None, tuple[str, ...]]:
    q = query.strip().lower()
    employer: str | None = None

    m = re.search(r"\b(?:at|from)\s+([a-z0-9][a-z0-9&'().,\- ]{1,100})", q)
    if m:
        employer = m.group(1)
    else:
        box_m = re.search(r"\bbox\s*\d{1,2}\s+([a-z0-9][a-z0-9&'().,\- ]{1,80})", q)
        if box_m:
            employer = box_m.group(1)
        else:
            for_m = re.search(r"\bfor\s+([a-z0-9][a-z0-9&'().,\- ]{1,100})", q)
            if for_m:
                employer = for_m.group(1)

    if employer is None:
        return None, tuple()

    employer = re.split(r"[?.!,;]", employer, maxsplit=1)[0]
    if year is not None:
        employer = re.sub(rf"\b(?:in|for|during)\s+{year}\b", "", employer)
    employer = " ".join(employer.split()).strip()
    tokens = tuple(
        t
        for t in re.findall(r"[a-z0-9][a-z0-9&'-]*", employer)
        if t not in _EMPLOYER_STOPWORDS and not re.fullmatch(r"20\d{2}", t)
    )
    if not tokens:
        return None, tuple()
    pretty = " ".join(t.upper() if len(t) <= 5 else t.title() for t in tokens)
    return pretty, tokens


def _is_tax_domain(query: str) -> bool:
    q = query.lower()
    return bool(
        re.search(r"\b(tax|w-?2|1099|1040|box\s*\d{1,2}|withheld|witheld|withholding|income|agi|wages|payroll|federal)\b", q)
        or re.search(r"\bhow\s+much\b[\s\S]{0,80}\b(make|made|earn|earned|pay|paid)\b", q)
    )


def parse_tax_query(query: str) -> TaxQuery | None:
    """Parse natural language tax asks into a structured deterministic request."""
    raw = (query or "").strip()
    if not raw:
        return None
    if not _is_tax_domain(raw):
        return None

    q = raw.lower()
    year = _parse_year(q)
    employer, employer_tokens = _parse_employer(q, year)

    box_m = re.search(r"\bbox\s*(\d{1,2})(?:\b|[a-z])", q)
    if box_m:
        box = int(box_m.group(1))
        return TaxQuery(
            raw_query=raw,
            tax_year=year,
            metric=METRIC_W2_BOX,
            form_type_hint="W2",
            field_code_hint=f"w2_box_{box}",
            employer=employer,
            employer_tokens=employer_tokens,
            box_number=box,
            interpretation=None,
        )

    if re.search(r"\btotal\s+tax\s+liabilit|\bline\s*24\b|\btotal\s+tax\b", q):
        return TaxQuery(raw, year, METRIC_TOTAL_TAX_LIABILITY, "1040", "f1040_line_24_total_tax", employer, employer_tokens, None, None)

    if re.search(r"\bpayroll\s+tax|social\s+security|medicare\b", q):
        return TaxQuery(raw, year, METRIC_PAYROLL_TAXES, "W2", None, employer, employer_tokens, None, None)

    if re.search(r"\bstate\s+tax|state\s+withholding|box\s*17\b", q):
        return TaxQuery(raw, year, METRIC_STATE_TAX, "W2", "w2_box_17_state_income_tax", employer, employer_tokens, None, None)

    if re.search(r"\badjusted\s+gross\s+income|\bagi\b|\bline\s*11\b", q):
        return TaxQuery(raw, year, METRIC_AGI, "1040", "f1040_line_11_agi", employer, employer_tokens, None, None)

    if re.search(
        r"\btaxes?\s+paid\b|\bpay\s+in\s+taxes\b|\bfederal\s+tax\s+withh?eld\b|\bfederal\s+wages?\s+withh?eld\b|\bwithh?eld\b|\bwitheld\b|\bwithholding\b",
        q,
    ):
        return TaxQuery(
            raw,
            year,
            METRIC_FEDERAL_WITHHELD,
            "W2",
            "w2_box_2_federal_income_tax_withheld",
            employer,
            employer_tokens,
            None,
            "Interpreting 'taxes paid' as federal income tax withheld (W-2 Box 2).",
        )

    if re.search(r"\bwages\b", q):
        return TaxQuery(raw, year, METRIC_WAGES, "W2", "w2_box_1_wages", employer, employer_tokens, None, None)

    if re.search(r"\bhow\s+much\b[\s\S]{0,80}\b(make|made|earn|earned)\b", q):
        return TaxQuery(raw, year, METRIC_TOTAL_INCOME, None, None, employer, employer_tokens, None, "Interpreting 'make/earn' as total income.")

    if re.search(r"\btotal\s+income\b|\bincome\b|\bline\s*9\b", q):
        return TaxQuery(raw, year, METRIC_TOTAL_INCOME, "1040", "f1040_line_9_total_income", employer, employer_tokens, None, None)

    return TaxQuery(raw, year, METRIC_TOTAL_INCOME, None, None, employer, employer_tokens, None, None)
