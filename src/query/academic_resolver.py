"""Deterministic academic course-history resolver from ingest-time row metadata."""
from __future__ import annotations

import re
from typing import Any, TypedDict

from style import dim, label_style
from query.formatting import render_sources_footer, style_answer
from query.academic import AcademicQuery


class _AcademicRow(TypedDict):
    confidence: str
    record_type: str
    course_code: str
    course_title: str
    course_term: str
    course_grade: str
    course_credits: str
    course_school: str
    student_name: str
    course_status: str
    source: str
    doc: str
    meta: dict[str, Any] | None


_RECORD_CONFIDENCE = {
    "transcript_row": "High",
    "audit_row": "Medium",
    "plan_row": "Low",
}
_RECORD_RANK = {"transcript_row": 3, "audit_row": 2, "plan_row": 1}
_TRANSCRIPT_SOURCE_DENYLIST = ("syllabus", "agreement", "transfer", "best choices", "four-year plan", "four year plan")
_UCCS_ALIAS_RE = re.compile(
    r"(uccs|cu\s+colo(?:rado)?\s+springs|university\s+of\s+colorado\s+colorado\s+springs)",
    re.IGNORECASE,
)


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


def _trusted_transcript_source(meta: dict[str, Any] | None) -> bool:
    src = str((meta or {}).get("source") or "").lower()
    if "transcript" in src:
        return True
    if any(tok in src for tok in _TRANSCRIPT_SOURCE_DENYLIST):
        return False
    # Conservative default: transcript_row should come from transcript-named files.
    return False


def _person_tokens(name: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z]{2,}", (name or "").lower())
        if tok not in {"student", "name"}
    }


def _names_overlap(lhs: str, rhs: str) -> bool:
    left = _person_tokens(lhs)
    right = _person_tokens(rhs)
    if not left or not right:
        return False
    overlap = left & right
    if len(overlap) >= 2:
        return True
    return bool(overlap and (len(left) == 1 or len(right) == 1))


def _normalize_school_key(value: str) -> str:
    raw = (value or "").lower()
    flat = re.sub(r"[^a-z0-9]", "", raw)
    if not flat:
        return ""
    if (
        "uccs" in flat
        or "cucolosprings" in flat
        or "cucoloradosprings" in flat
        or "universityofcoloradocoloradosprings" in flat
    ):
        return "uccs"
    return flat


def _row_matches_school(row: _AcademicRow, requested_school: str | None) -> bool:
    key = _normalize_school_key(requested_school or "")
    if not key:
        return True
    for candidate in (row.get("course_school") or "", row.get("source") or ""):
        ckey = _normalize_school_key(candidate)
        if not ckey:
            continue
        if ckey == key:
            return True
    if key == "uccs":
        src = str(row.get("source") or "")
        school = str(row.get("course_school") or "")
        if _UCCS_ALIAS_RE.search(src) or _UCCS_ALIAS_RE.search(school):
            return True
    return False


def _format_course_row(row: _AcademicRow) -> str:
    left = " ".join(x for x in [row["course_code"], row["course_title"]] if x).strip()
    details: list[str] = []
    if row["course_term"]:
        details.append(f"Term: {row['course_term']}")
    if row["course_credits"]:
        details.append(f"Credits: {row['course_credits']}")
    if row["course_grade"]:
        details.append(f"Grade: {row['course_grade']}")
    if row["course_status"] == "attempted_not_completed":
        details.append("Status: attempted_not_completed")
    if details:
        return f"[{row['confidence']}] {left} ({'; '.join(details)})".strip()
    return f"[{row['confidence']}] {left}".strip()


def _with_footer(
    *,
    answer: str,
    source_label: str,
    no_color: bool,
    source_rows: list[tuple[str, dict[str, Any] | None]],
    detailed_sources: bool = False,
) -> str:
    out = [
        style_answer(answer, no_color),
        "",
        dim(no_color, "---"),
        label_style(no_color, f"Answered by: {source_label}"),
    ]
    if source_rows:
        docs = [doc for doc, _meta in source_rows]
        metas = [meta for _doc, meta in source_rows]
        out.extend(
            [""]
            + render_sources_footer(
                docs,
                metas,
                [None for _ in source_rows],
                no_color=no_color,
                detailed=detailed_sources,
                max_items=10,
            )
        )
    return "\n".join(out)


def run_academic_resolver(
    *,
    query_contract: AcademicQuery,
    collection: Any,
    use_unified: bool,
    silo: str | None,
    source_label: str,
    no_color: bool,
    user_name: str | None = None,
    explain: bool = False,
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
        if record_type == "transcript_row" and not _trusted_transcript_source(meta):
            # Ignore suspicious transcript rows from non-transcript sources.
            continue
        row: _AcademicRow = {
            "confidence": _RECORD_CONFIDENCE[record_type],
            "record_type": record_type,
            "course_code": _normalize_field((meta or {}).get("course_code")),
            "course_title": _normalize_field((meta or {}).get("course_title")),
            "course_term": _normalize_field((meta or {}).get("course_term")),
            "course_grade": _normalize_field((meta or {}).get("course_grade")),
            "course_credits": _normalize_field((meta or {}).get("course_credits")),
            "course_school": _normalize_field((meta or {}).get("course_school")),
            "student_name": _normalize_field((meta or {}).get("student_name")),
            "course_status": _normalize_field((meta or {}).get("course_status")) or "attempted",
            "source": _normalize_field((meta or {}).get("source")),
            "doc": doc,
            "meta": meta,
        }
        if not row["course_code"] and not row["course_title"]:
            continue
        rows.append(row)

    if not rows:
        return None

    academic_rows_pre_filter = len(rows)
    identity_name = _normalize_field(user_name)
    rows_after_identity = list(rows)
    if identity_name:
        filtered: list[_AcademicRow] = []
        for row in rows_after_identity:
            if row["record_type"] != "transcript_row":
                filtered.append(row)
                continue
            student_name = row.get("student_name") or ""
            if student_name and _names_overlap(student_name, identity_name):
                filtered.append(row)
        rows_after_identity = filtered

    requested_school = _normalize_field(query_contract.get("requested_school"))
    rows_after_school = [r for r in rows_after_identity if _row_matches_school(r, requested_school)]

    if not rows_after_school and (identity_name or requested_school):
        reason_parts: list[str] = []
        if identity_name:
            reason_parts.append(f"configured identity: {identity_name}")
        if requested_school:
            reason_parts.append(f"requested school: {requested_school}")
        reason = "; ".join(reason_parts)
        text = "I found academic course rows in this scope, but none matched the requested constraints."
        if reason:
            text = f"{text}\n{reason}."
        source_rows = [(r.get("doc") or "", r.get("meta")) for r in rows[:5]]
        return {
            "response": _with_footer(
                answer=text,
                source_label=source_label,
                no_color=no_color,
                source_rows=source_rows,
                detailed_sources=explain,
            ),
            "guardrail_no_match": True,
            "guardrail_reason": "academic_identity_school_mismatch",
            "requested_year": query_contract.get("requested_year"),
            "requested_form": "academic_history",
            "requested_line": None,
            "num_docs": 0,
            "receipt_metas": [r.get("meta") for r in rows if r.get("meta")][:20],
            "academic_mode": True,
            "academic_rerank_applied": False,
            "academic_transcript_hits": 0,
            "academic_evidence_rows": 0,
            "academic_identity_name": (identity_name or None),
            "academic_identity_rows": len(rows_after_identity),
            "academic_school_rows": len(rows_after_school),
            "academic_rows_pre_filter": academic_rows_pre_filter,
        }

    if not rows_after_school:
        return None

    deduped: dict[tuple[str, str, str, str], _AcademicRow] = {}
    for row in rows_after_school:
        key = _dedupe_key(row)
        if key not in deduped:
            deduped[key] = row
            continue
        prev = deduped[key]
        if _RECORD_RANK.get(row["record_type"], 0) > _RECORD_RANK.get(prev["record_type"], 0):
            deduped[key] = row
    ordered_rows = sorted(deduped.values(), key=_row_sort_key)

    answer_lines: list[str] = []
    for i, row in enumerate(ordered_rows, start=1):
        answer_lines.append(f"{i}. {_format_course_row(row)}")
    answer = "\n".join(answer_lines)

    source_rows: list[tuple[str, dict[str, Any] | None]] = []
    seen_source_keys: set[tuple[str, str]] = set()
    for row in ordered_rows:
        src = (row.get("source") or "").strip()
        key = (src, str((row.get("meta") or {}).get("page") or ""))
        if key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        source_rows.append((row.get("doc") or "", row.get("meta")))

    transcript_hits = sum(1 for r in ordered_rows if r["record_type"] == "transcript_row")
    return {
        "response": _with_footer(
            answer=answer,
            source_label=source_label,
            no_color=no_color,
            source_rows=source_rows,
            detailed_sources=explain,
        ),
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
        "academic_identity_name": (identity_name or None),
        "academic_identity_rows": len(rows_after_identity),
        "academic_school_rows": len(rows_after_school),
        "academic_rows_pre_filter": academic_rows_pre_filter,
    }
