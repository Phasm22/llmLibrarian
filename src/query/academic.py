"""Deterministic academic query contract parsing."""
from __future__ import annotations

import re
from typing import TypedDict


class AcademicQuery(TypedDict):
    mode: str
    completed_only: bool
    requested_school: str | None
    requested_term: str | None
    requested_year: str | None


_COURSE_WORDS = re.compile(r"\b(class(?:es)?|course(?:s)?)\b", re.IGNORECASE)
_HISTORY_WORDS = re.compile(
    r"\b(course\s+history|transcript|completed|taken|have\s+i\s+taken|did\s+i\s+take|classes?\s+have\s+i\s+taken)\b",
    re.IGNORECASE,
)
_PLANNING_WORDS = re.compile(r"\b(should|recommend|recommended|suggest(?:ed)?)\b", re.IGNORECASE)
_TERM_RE = re.compile(r"\b(Spring|Summer|Fall|Winter)\s+(20\d{2})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_SCHOOL_RE = re.compile(
    r"\b(?:at|from|in)\s+([A-Za-z0-9][A-Za-z0-9 .&'_-]{1,64})",
    re.IGNORECASE,
)


def _normalize_school(raw: str | None) -> str | None:
    if not raw:
        return None
    school = re.split(
        r"\b(?:for|with|where|who|when|why|how|and|but|then|that)\b",
        raw.strip(),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.;:!?")
    if not school:
        return None
    return school


def parse_academic_query(query: str) -> AcademicQuery | None:
    """
    Parse class-history asks into a deterministic academic query contract.
    Returns None when query is not class-history/completion intent.
    """
    q = (query or "").strip()
    if not q:
        return None
    ql = q.lower()
    has_course = bool(_COURSE_WORDS.search(ql))
    has_history = bool(_HISTORY_WORDS.search(ql))
    if not (has_course and has_history):
        return None
    if _PLANNING_WORDS.search(ql) and not re.search(r"\b(taken|completed|history|transcript)\b", ql):
        return None

    term_match = _TERM_RE.search(q)
    requested_term = None
    if term_match:
        requested_term = f"{term_match.group(1).title()} {term_match.group(2)}"

    year_match = _YEAR_RE.search(q)
    school_match = _SCHOOL_RE.search(q)

    return {
        "mode": "classes_taken",
        "completed_only": True,
        "requested_school": _normalize_school(school_match.group(1) if school_match else None),
        "requested_term": requested_term,
        "requested_year": (year_match.group(1) if year_match else None),
    }
