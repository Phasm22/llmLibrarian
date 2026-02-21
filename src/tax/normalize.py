"""Normalization helpers for tax ledger extraction and resolver logic."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from tax.schema import FORM_1040, FORM_1099_B, FORM_1099_DIV, FORM_1099_INT, FORM_W2

_OCR_LABEL = "ocr text (scan fallback):"
_ENTITY_STOPWORDS = {
    "the",
    "and",
    "for",
    "from",
    "at",
    "in",
    "llc",
    "inc",
    "co",
    "corp",
    "corporation",
    "company",
    "ltd",
    "form",
    "w2",
    "w-2",
    "federal",
    "state",
    "tax",
}


def normalize_money_value(value: str) -> str | None:
    """Return normalized decimal string with two decimals, or None for non-money values."""
    raw = (value or "").strip().replace("$", "")
    raw = re.sub(r"[^0-9,.-]", "", raw)
    if not raw:
        return None
    raw = raw.replace(",", "")
    if raw.startswith("."):
        raw = f"0{raw}"
    if raw in {"-", ".", ""}:
        return None
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return f"{dec.quantize(Decimal('0.01'))}"


def format_money_decimal(normalized: str) -> str:
    """Render normalized decimal string with comma separators."""
    try:
        dec = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return normalized
    return f"{dec:,.2f}"


def parse_decimal(normalized: str) -> Decimal | None:
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def extract_tax_year(source: str, text: str) -> int | None:
    """Extract tax year from source path first, then text body."""
    src = source or ""
    src_years = re.findall(r"\b(20\d{2})\b", src)
    if src_years:
        return int(src_years[-1])
    body = text or ""
    body_years = re.findall(r"\b(20\d{2})\b", body)
    if body_years:
        return int(body_years[0])
    return None


def infer_form_type(source: str, text: str) -> str | None:
    hay = f"{source}\n{text}".lower()
    if "w-2" in hay or " form w2" in hay or re.search(r"\bw2\b", hay):
        return FORM_W2
    if "form 1040" in hay or re.search(r"\b1040(?:-sr)?\b", hay):
        return FORM_1040
    if "1099-int" in hay:
        return FORM_1099_INT
    if "1099-div" in hay:
        return FORM_1099_DIV
    if "1099-b" in hay:
        return FORM_1099_B
    if "1099" in hay:
        return "1099"
    return None


def is_tax_document(source: str, text: str) -> bool:
    return infer_form_type(source, text) is not None


def is_ocr_text(text: str) -> bool:
    return _OCR_LABEL in (text or "").lower()


def tokenize_entity(value: str) -> list[str]:
    tokens = [
        t
        for t in re.findall(r"[a-z0-9][a-z0-9&'-]*", (value or "").lower())
        if t not in _ENTITY_STOPWORDS and not re.fullmatch(r"20\d{2}", t)
    ]
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def infer_entity_name(source: str, text: str) -> str:
    """Prefer employer/payer labels from text, fallback to source filename stem."""
    for pat in (
        r"(?im)\bemployer(?:'?s)?\s+name\s*[:\-]\s*([^\n]+)",
        r"(?im)^\s*Employer\s*[:\-]\s*([^\n]+)",
        r"(?im)\bpayer\s*[:\-]\s*([^\n]+)",
    ):
        m = re.search(pat, text or "")
        if m:
            candidate = " ".join((m.group(1) or "").strip().split())
            if candidate:
                return candidate
    stem = re.sub(r"\.[^.]+$", "", (source or "").split("/")[-1])
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b20\d{2}\b", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\b(form|federal|state|income|tax|return|w2|w-2|1099|copy|statement)\b", "", stem, flags=re.IGNORECASE)
    stem = " ".join(stem.split())
    return stem or "Unknown"


def source_tokens(source: str) -> list[str]:
    return tokenize_entity((source or "").replace("/", " "))


def build_trace_ref(source: str, page: int) -> str:
    return f"{source}#page={page}"


def build_record_id(*parts: Iterable[str] | str) -> str:
    flat: list[str] = []
    for part in parts:
        if isinstance(part, str):
            flat.append(part)
        else:
            flat.extend(str(x) for x in part)
    payload = "|".join(flat)
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:24]
