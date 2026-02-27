"""Deterministic academic course-history resolver from ingest-time row metadata."""
from __future__ import annotations

from typing import Any, TypedDict

from style import bold, dim, label_style
from query.formatting import format_source, style_answer
from query.academic import AcademicQuery


class _AcademicRow(TypedDict):
    confidence: str
    record_type: str
    course_code: str
    course_title: str
    course_term: str
    course_grade: str
    course_credits: str
    source: str
    doc: str
    meta: dict[str, Any] | None


_RECORD_CONFIDENCE = {
    "transcript_row": "High",
    "audit_row": "Medium",
    "plan_row": "Low",
}
_RECORD_RANK = {"transcript_row": 3, "audit_row": 2, "plan_row": 1}


def _flatten_get_list(values: Any) -> list[Any]:
    if not values:
        return []
    if isinstance(values, list) and values and isinstance(values[0], list):
        return list(values[0])
    if isinstance(values, list):
        return list(values)
    return []


def _normalize_field(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_key(row: _AcademicRow) -> tuple[str, str, str, str]:
    return (
        row["course_code"].lower(),
        row["course_title"].lower(),
        row["course_term"].lower(),
        row["source"].lower(),
    )


def _row_sort_key(row: _AcademicRow) -> tuple[str, str, str, str]:
    return (
        row["course_term"].lower(),
        row["course_code"].lower(),
        row["course_title"].lower(),
        row["source"].lower(),
    )


def _format_course_row(row: _AcademicRow) -> str:
    left = " ".join(x for x in [row["course_code"], row["course_title"]] if x).strip()
    details: list[str] = []
    if row["course_term"]:
        details.append(f"Term: {row['course_term']}")
    if row["course_credits"]:
        details.append(f"Credits: {row['course_credits']}")
    if row["course_grade"]:
        details.append(f"Grade: {row['course_grade']}")
    if details:
        return f"[{row['confidence']}] {left} ({'; '.join(details)})".strip()
    return f"[{row['confidence']}] {left}".strip()


def run_academic_resolver(
    *,
    query_contract: AcademicQuery,
    collection: Any,
    use_unified: bool,
    silo: str | None,
    source_label: str,
    no_color: bool,
) -> dict[str, Any] | None:
    """
    Resolve class-history asks from deterministic course-row metadata.
    Returns None when no academic row records are available in scope.
    """
    where_parts: list[dict[str, Any]] = [{"record_type": {"$in": ["transcript_row", "audit_row", "plan_row"]}}]
    if use_unified and silo:
        where_parts.append({"silo": silo})
    where: dict[str, Any]
    if len(where_parts) == 1:
        where = where_parts[0]
    else:
        where = {"$and": where_parts}

    try:
        result = collection.get(where=where, include=["documents", "metadatas"])
    except Exception:
        return None

    docs_all = _flatten_get_list(result.get("documents"))
    metas_all = _flatten_get_list(result.get("metadatas"))
    rows: list[_AcademicRow] = []
    for i, doc_raw in enumerate(docs_all):
        doc = str(doc_raw or "")
        meta_raw = metas_all[i] if i < len(metas_all) else None
        meta = meta_raw if isinstance(meta_raw, dict) else None
        record_type = _normalize_field((meta or {}).get("record_type")).lower()
        if record_type not in _RECORD_CONFIDENCE:
            continue
        row: _AcademicRow = {
            "confidence": _RECORD_CONFIDENCE[record_type],
            "record_type": record_type,
            "course_code": _normalize_field((meta or {}).get("course_code")),
            "course_title": _normalize_field((meta or {}).get("course_title")),
            "course_term": _normalize_field((meta or {}).get("course_term")),
            "course_grade": _normalize_field((meta or {}).get("course_grade")),
            "course_credits": _normalize_field((meta or {}).get("course_credits")),
            "source": _normalize_field((meta or {}).get("source")),
            "doc": doc,
            "meta": meta,
        }
        if not row["course_code"] and not row["course_title"]:
            continue
        rows.append(row)

    if not rows:
        return None

    deduped: dict[tuple[str, str, str, str], _AcademicRow] = {}
    for row in rows:
        key = _dedupe_key(row)
        if key not in deduped:
            deduped[key] = row
            continue
        prev = deduped[key]
        if _RECORD_RANK.get(row["record_type"], 0) > _RECORD_RANK.get(prev["record_type"], 0):
            deduped[key] = row
    ordered_rows = sorted(deduped.values(), key=_row_sort_key)

    answer_lines = ["Here are the classes found in your indexed academic records:"]
    for i, row in enumerate(ordered_rows, start=1):
        answer_lines.append(f"{i}. {_format_course_row(row)}")
    answer = style_answer("\n".join(answer_lines), no_color)

    source_rows: list[tuple[str, dict | None]] = []
    seen_source_keys: set[tuple[str, str]] = set()
    for row in ordered_rows:
        src = (row.get("source") or "").strip()
        key = (src, str((row.get("meta") or {}).get("page") or ""))
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        source_rows.append((row.get("doc") or "", row.get("meta")))

    out = [
        answer,
        "",
        dim(no_color, "---"),
        label_style(no_color, f"Answered by: {source_label}"),
        "",
        bold(no_color, "Sources:"),
    ]
    for doc, meta in source_rows[:10]:
        out.append(format_source(doc, meta, None, no_color=no_color))

    transcript_hits = sum(1 for r in ordered_rows if r["record_type"] == "transcript_row")
    return {
        "response": "\n".join(out),
        "guardrail_no_match": False,
        "guardrail_reason": "academic_history_rows",
        "requested_year": query_contract.get("requested_year"),
        "requested_form": "academic_history",
        "requested_line": None,
        "num_docs": len(ordered_rows),
        "receipt_metas": [r.get("meta") for r in ordered_rows if r.get("meta")][:20],
        "academic_mode": True,
        "academic_rerank_applied": False,
        "academic_transcript_hits": transcript_hits,
        "academic_evidence_rows": len(ordered_rows),
    }
