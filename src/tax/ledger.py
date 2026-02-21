"""Tax ledger extraction + persistence.

The ledger is an ingest-time normalized row store used by deterministic tax QA.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from tax.extractors.form_fields import extract_form_fields
from tax.extractors.layout import extract_layout_fields
from tax.extractors.ocr_layout import extract_ocr_layout_fields
from tax.normalize import (
    build_record_id,
    build_trace_ref,
    extract_tax_year,
    infer_entity_name,
    infer_form_type,
    is_ocr_text,
    is_tax_document,
    source_tokens,
    tokenize_entity,
)
from tax.schema import TaxLedgerRow

_LEDGER_FILENAME = "tax_ledger.json"


def ledger_path(db_path: str | Path) -> Path:
    return Path(db_path) / _LEDGER_FILENAME


def load_tax_ledger_rows(
    db_path: str | Path,
    *,
    silo: str | None = None,
    tax_year: int | None = None,
) -> list[TaxLedgerRow]:
    rows = _read_all_rows(db_path)
    if silo is not None:
        rows = [r for r in rows if str(r.get("silo") or "") == silo]
    if tax_year is not None:
        rows = [r for r in rows if int(r.get("tax_year") or 0) == tax_year]
    return rows


def replace_tax_rows_for_sources(
    db_path: str | Path,
    *,
    silo: str,
    sources: set[str],
    new_rows: list[TaxLedgerRow],
    replace_all_in_silo: bool = False,
) -> None:
    """Replace all rows for the provided source paths in a silo, then append new rows."""
    existing = _read_all_rows(db_path)
    if replace_all_in_silo:
        existing = [row for row in existing if str(row.get("silo") or "") != silo]
    elif sources:
        existing = [
            row
            for row in existing
            if not _row_matches_source(row, silo=silo, sources=sources)
        ]
    merged = [*existing, *new_rows]
    _write_all_rows(db_path, merged)


def extract_tax_rows_from_chunks(
    chunks: list[tuple[str, str, dict[str, Any]]],
    *,
    created_at: str | None = None,
) -> list[TaxLedgerRow]:
    """Build normalized tax ledger rows from per-page chunk text and metadata."""
    now_iso = created_at or datetime.now(timezone.utc).isoformat()
    out: list[TaxLedgerRow] = []
    seen: set[str] = set()

    for chunk_id, doc, meta in chunks:
        text = str(doc or "")
        source = str((meta or {}).get("source") or "")
        silo = str((meta or {}).get("silo") or "")
        page = int((meta or {}).get("page") or 1)
        doc_hash = str((meta or {}).get("file_hash") or (meta or {}).get("chunk_hash") or chunk_id)
        if not source or not silo or not text:
            continue
        if not is_tax_document(source, text):
            continue

        year = extract_tax_year(source, text)
        if year is None:
            continue
        form_type_hint = infer_form_type(source, text)

        entity_name = infer_entity_name(source, text)
        tokens = list(dict.fromkeys([*tokenize_entity(entity_name), *source_tokens(source)]))

        extracted: list[dict[str, Any]] = []
        extracted.extend(extract_form_fields(text, form_type_hint=form_type_hint))
        if is_ocr_text(text):
            extracted.extend(extract_ocr_layout_fields(text, form_type_hint=form_type_hint))
        else:
            extracted.extend(extract_layout_fields(text, form_type_hint=form_type_hint))

        for row in extracted:
            record_id = build_record_id(
                silo,
                source,
                str(page),
                str(year),
                str(row.get("form_type") or ""),
                str(row.get("field_code") or ""),
                str(row.get("normalized_decimal") or ""),
                doc_hash,
            )
            if record_id in seen:
                continue
            seen.add(record_id)
            out.append(
                TaxLedgerRow(
                    record_id=record_id,
                    silo=silo,
                    source=source,
                    page=page,
                    doc_hash=doc_hash,
                    tax_year=year,
                    form_type=str(row.get("form_type") or (form_type_hint or "UNKNOWN")),
                    field_code=str(row.get("field_code") or ""),
                    field_label=str(row.get("field_label") or ""),
                    entity_name=entity_name,
                    entity_tokens=tokens,
                    raw_value=str(row.get("raw_value") or ""),
                    normalized_decimal=str(row.get("normalized_decimal") or ""),
                    currency="USD",
                    extractor_tier=str(row.get("extractor_tier") or "layout"),
                    confidence=float(row.get("confidence") or 0.0),
                    trace_ref=build_trace_ref(source, page),
                    created_at=now_iso,
                )
            )

    return out


def _row_matches_source(row: dict[str, Any], *, silo: str, sources: set[str]) -> bool:
    if str(row.get("silo") or "") != silo:
        return False
    src = str(row.get("source") or "")
    if src in sources:
        return True
    # ZIP source labels are normalized as "<zip path> > <inner path>".
    for base in sources:
        if src.startswith(f"{base} > "):
            return True
    return False


def _read_all_rows(db_path: str | Path) -> list[TaxLedgerRow]:
    path = ledger_path(db_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[TaxLedgerRow] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)  # type: ignore[arg-type]
    return out


def _write_all_rows(db_path: str | Path, rows: list[TaxLedgerRow]) -> None:
    path = ledger_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
