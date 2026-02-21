"""Tier C extractor: OCR text routed through layout extraction heuristics."""
from __future__ import annotations

import re
from typing import Any

from tax.extractors.layout import extract_layout_fields


def _clean_ocr_text(text: str) -> str:
    body = text or ""
    # Common OCR confusions around tax forms.
    body = re.sub(r"(?i)\biox\b", "box", body)
    body = re.sub(r"(?i)\bwage\s+and\s+rax\b", "wage and tax", body)
    body = re.sub(r"(?i)\bfedera1\b", "federal", body)
    body = re.sub(r"(?i)\bmedlcare\b", "medicare", body)
    return body


def extract_ocr_layout_fields(text: str, form_type_hint: str | None = None) -> list[dict[str, Any]]:
    cleaned = _clean_ocr_text(text)
    return extract_layout_fields(
        cleaned,
        form_type_hint=form_type_hint,
        tier="ocr_layout",
        base_confidence=0.74,
    )
