"""
Filename-embedded date parsing and natural-language date-range resolution.

Two responsibilities:

1. ``parse_filename_date(path)`` — at ingest, extract a stable date from a file's
   basename if one is present. Only patterns that include an explicit year are
   accepted, since stored values must not drift across days. Returns
   ``(iso_str | None, "day" | "month" | None)``.

2. ``resolve_query_date_range(query, today)`` — at query time, turn a natural
   phrase like "today's journal" or "last Monday" into an inclusive
   ``(start, end)`` date pair in the user's local timezone. Year-less forms
   ("May 6", "5/6") are accepted here since "today" supplies the year.

A small ``query_has_date_phrase`` helper is exposed for the intent detector;
it shares the phrase set with the resolver so both stay aligned.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

Precision = Literal["day", "month"]

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTH_ALT = "|".join(sorted(_MONTHS.keys(), key=len, reverse=True))

# Filename patterns. Each pattern must yield a complete date (year present).
# Anchored to start-of-basename or a non-digit separator so embedded years like
# the "2025" in "2026-05-06_notes_about_2025.md" don't get picked up as month
# patterns. Day-precision is tried first.
_FN_DAY_DASH = re.compile(r"(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})(?!\d)")
_FN_DAY_COMPACT = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
_FN_DAY_MONTHNAME = re.compile(
    rf"(?<![A-Za-z])({_MONTH_ALT})\s+(\d{{1,2}}),\s*(\d{{4}})(?!\d)",
    re.IGNORECASE,
)
_FN_MONTH_DASH = re.compile(r"(?<!\d)(\d{4})[-_](\d{2})(?!\d)")


def parse_filename_date(path: str | Path) -> tuple[str | None, Precision | None]:
    """Return ``(iso_date, precision)`` for the first valid date in the basename.

    Day-precision patterns are tried first. Month-precision is stored as
    first-of-month with precision ``"month"``. ``(None, None)`` if nothing
    matches or the matched values are not a valid date.
    """
    name = Path(path).name
    if not name:
        return (None, None)

    # If any day-precision regex matches at all, this file is a day-precision
    # attempt — succeed or fail there. Don't silently downgrade to month
    # precision when a day-shaped name has invalid month/day values
    # (e.g. "2026-02-30.md").
    day_attempted = False

    m = _FN_DAY_DASH.search(name)
    if m:
        day_attempted = True
        iso = _validate_ymd(m.group(1), m.group(2), m.group(3))
        if iso:
            return (iso, "day")

    m = _FN_DAY_COMPACT.search(name)
    if m:
        day_attempted = True
        iso = _validate_ymd(m.group(1), m.group(2), m.group(3))
        if iso:
            return (iso, "day")

    m = _FN_DAY_MONTHNAME.search(name)
    if m:
        day_attempted = True
        month = _MONTHS.get(m.group(1).lower())
        if month is not None:
            iso = _validate_ymd(m.group(3), f"{month:02d}", m.group(2).zfill(2))
            if iso:
                return (iso, "day")

    if day_attempted:
        return (None, None)

    m = _FN_MONTH_DASH.search(name)
    if m and (iso := _validate_ymd(m.group(1), m.group(2), "01")):
        return (iso, "month")

    return (None, None)


def _validate_ymd(y: str, mo: str, d: str) -> str | None:
    try:
        dt = date(int(y), int(mo), int(d))
    except ValueError:
        return None
    return dt.isoformat()


# --- Query-time date phrase detection and range resolution ---

# Date phrase regex shared by the intent detector and the resolver. Order matters
# in resolution but not in detection; here we want to know whether *any* of these
# phrase types is present.
_DATE_PHRASE_RE = re.compile(
    r"\b(today|yesterday|tomorrow)\b"
    r"|\b(this|last|next)\s+(week|month|"
    + "|".join(_WEEKDAYS.keys())
    + r")\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b({_MONTH_ALT})\s+\d{{1,2}}(?:,\s*\d{{4}})?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{4})?\b",
    re.IGNORECASE,
)


def query_has_date_phrase(query: str) -> bool:
    """True if the query contains any phrase the resolver can interpret."""
    if not query:
        return False
    return bool(_DATE_PHRASE_RE.search(query))


def resolve_query_date_range(
    query: str,
    today: date | None = None,
) -> tuple[date, date] | None:
    """Resolve the first date phrase in ``query`` to an inclusive ``(start, end)``.

    Local-time semantics: ``today`` defaults to ``date.today()``. Year-less forms
    inherit ``today.year``. Returns ``None`` if no phrase resolves.
    """
    if not query:
        return None
    today = today or date.today()
    q = query.lower()

    # Explicit ISO date: 2026-05-06
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (d, d)
        except ValueError:
            pass

    # today / yesterday / tomorrow
    if re.search(r"\btoday\b", q):
        return (today, today)
    if re.search(r"\byesterday\b", q):
        d = today - timedelta(days=1)
        return (d, d)
    if re.search(r"\btomorrow\b", q):
        d = today + timedelta(days=1)
        return (d, d)

    # this/last/next week|month
    m = re.search(r"\b(this|last|next)\s+(week|month)\b", q)
    if m:
        which, unit = m.group(1), m.group(2)
        if unit == "week":
            return _week_bounds(today, which)
        return _month_bounds(today, which)

    # this/last/next <weekday>
    m = re.search(rf"\b(this|last|next)\s+({'|'.join(_WEEKDAYS.keys())})\b", q)
    if m:
        which, wd = m.group(1), m.group(2)
        target = _resolve_weekday(today, _WEEKDAYS[wd], which)
        return (target, target)

    # "May 6, 2026" or "May 6"
    m = re.search(rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?\b", q)
    if m:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(year, month, day)
            return (d, d)
        except ValueError:
            pass

    # "5/6" or "5/6/2026"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", q)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = date(year, month, day)
            return (d, d)
        except ValueError:
            pass

    return None


def _week_bounds(today: date, which: str) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    if which == "last":
        monday = monday - timedelta(days=7)
    elif which == "next":
        monday = monday + timedelta(days=7)
    return (monday, monday + timedelta(days=6))


def _month_bounds(today: date, which: str) -> tuple[date, date]:
    if which == "last":
        last_of_prev = today.replace(day=1) - timedelta(days=1)
        first = last_of_prev.replace(day=1)
        return (first, last_of_prev)
    if which == "next":
        if today.month == 12:
            first = date(today.year + 1, 1, 1)
        else:
            first = date(today.year, today.month + 1, 1)
        last = (_first_of_month_after(first)) - timedelta(days=1)
        return (first, last)
    first = today.replace(day=1)
    last = _first_of_month_after(first) - timedelta(days=1)
    return (first, last)


def _first_of_month_after(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _resolve_weekday(today: date, weekday: int, which: str) -> date:
    """Resolve "last/this/next <weekday>" to a concrete date.

    "this Monday" → the Monday of the current week (may be today, may be future).
    "last Monday" → the most recent past Monday strictly before today (so "last
    Monday" on a Monday returns 7 days ago, never today).
    "next Monday" → the next Monday strictly after today.
    """
    delta = weekday - today.weekday()
    if which == "this":
        return today + timedelta(days=delta)
    if which == "last":
        if delta >= 0:
            delta -= 7
        return today + timedelta(days=delta)
    if delta <= 0:
        delta += 7
    return today + timedelta(days=delta)


def month_overlaps_range(
    iso_first_of_month: str,
    range_start: date,
    range_end: date,
) -> bool:
    """True if a month-precision date's full calendar month overlaps the range.

    Per design: month-precision matches only when the range covers the entire
    month, not when it merely touches it. Used by ``op_find_files``.
    """
    try:
        month_first = date.fromisoformat(iso_first_of_month)
    except ValueError:
        return False
    month_last = _first_of_month_after(month_first) - timedelta(days=1)
    return range_start <= month_first and range_end >= month_last


def utc_mtime_to_local_date(mtime: float) -> str:
    """Convert a UTC epoch float to an ISO date string in the user's local tz."""
    if mtime <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(mtime)).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return ""
