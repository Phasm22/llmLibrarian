from datetime import date

import pytest

from query.filename_dates import (
    month_overlaps_range,
    parse_filename_date,
    query_has_date_phrase,
    resolve_query_date_range,
    utc_mtime_to_local_date,
)


# --- parse_filename_date ---

@pytest.mark.parametrize(
    "name,expected_iso,expected_precision",
    [
        ("2026-05-06.md", "2026-05-06", "day"),
        ("2026_05_06.md", "2026-05-06", "day"),
        ("20260506.md", "2026-05-06", "day"),
        ("journal-2026-05-06.md", "2026-05-06", "day"),
        ("notes_2026-05-06_v2.md", "2026-05-06", "day"),
        ("May 6, 2026 notes.md", "2026-05-06", "day"),
        ("Jul 4, 2024 cookout.txt", "2024-07-04", "day"),
        ("2026-05.md", "2026-05-01", "month"),
        ("summary_2026_05.md", "2026-05-01", "month"),
    ],
)
def test_parse_filename_date_known_forms(name, expected_iso, expected_precision):
    iso, precision = parse_filename_date(name)
    assert iso == expected_iso
    assert precision == expected_precision


def test_parse_filename_date_leading_date_wins_over_embedded_year():
    # The trap: embedded "2025" comes after a valid leading day-precision date.
    iso, precision = parse_filename_date("2026-05-06_notes_about_2025.md")
    assert iso == "2026-05-06"
    assert precision == "day"


def test_parse_filename_date_invalid_dates_reject():
    assert parse_filename_date("2026-13-45.md") == (None, None)
    assert parse_filename_date("2026-02-30.md") == (None, None)
    assert parse_filename_date("20260231.md") == (None, None)


def test_parse_filename_date_ambiguous_locale_rejected():
    # MM-DD-YYYY and DD-MM-YYYY are intentionally not recognized.
    assert parse_filename_date("05-06-2026.md") == (None, None)
    assert parse_filename_date("06-05-2026.md") == (None, None)


def test_parse_filename_date_bare_year_not_a_date():
    assert parse_filename_date("notes_2024.md") == (None, None)
    assert parse_filename_date("2024.md") == (None, None)


def test_parse_filename_date_yyyymmdd_requires_clean_boundary():
    # Adjacent digits should not be treated as part of the date.
    assert parse_filename_date("123202605061.md") == (None, None)


def test_parse_filename_date_handles_path_object():
    from pathlib import Path
    iso, precision = parse_filename_date(Path("/var/data/journal/2026-05-06.md"))
    assert iso == "2026-05-06"
    assert precision == "day"


def test_parse_filename_date_empty_path():
    assert parse_filename_date("") == (None, None)


# --- query_has_date_phrase ---

@pytest.mark.parametrize(
    "query",
    [
        "today's journal entry",
        "yesterday's notes",
        "last Monday's log",
        "this week's files",
        "May 6, 2026 entry",
        "5/6 notes",
        "the entry from 2026-05-06",
    ],
)
def test_query_has_date_phrase_positive(query):
    assert query_has_date_phrase(query)


@pytest.mark.parametrize(
    "query",
    [
        "what is my most common language",
        "files from 2024",  # bare year is not a date phrase here
        "show me the report",
        "",
    ],
)
def test_query_has_date_phrase_negative(query):
    assert not query_has_date_phrase(query)


# --- resolve_query_date_range ---

TODAY = date(2026, 5, 6)  # a Wednesday


def test_resolve_today():
    assert resolve_query_date_range("today's journal", TODAY) == (TODAY, TODAY)


def test_resolve_yesterday():
    expected = date(2026, 5, 5)
    assert resolve_query_date_range("yesterday's note", TODAY) == (expected, expected)


def test_resolve_tomorrow():
    expected = date(2026, 5, 7)
    assert resolve_query_date_range("tomorrow's plan", TODAY) == (expected, expected)


def test_resolve_iso_date_explicit():
    assert resolve_query_date_range("the 2026-05-06 entry", TODAY) == (TODAY, TODAY)


def test_resolve_this_week_monday_to_sunday():
    # 2026-05-06 is a Wednesday -> Mon = 2026-05-04, Sun = 2026-05-10
    start, end = resolve_query_date_range("this week's notes", TODAY)
    assert start == date(2026, 5, 4)
    assert end == date(2026, 5, 10)


def test_resolve_last_week():
    start, end = resolve_query_date_range("last week's files", TODAY)
    assert start == date(2026, 4, 27)
    assert end == date(2026, 5, 3)


def test_resolve_this_month():
    start, end = resolve_query_date_range("this month's entries", TODAY)
    assert start == date(2026, 5, 1)
    assert end == date(2026, 5, 31)


def test_resolve_last_month_february_boundary():
    today_march = date(2026, 3, 5)
    start, end = resolve_query_date_range("last month's notes", today_march)
    assert start == date(2026, 2, 1)
    assert end == date(2026, 2, 28)


def test_resolve_last_monday_strictly_before_today():
    today_monday = date(2026, 5, 4)  # Monday
    # "last Monday" on a Monday is 7 days ago, not today.
    start, end = resolve_query_date_range("last Monday's log", today_monday)
    assert start == date(2026, 4, 27)
    assert end == date(2026, 4, 27)


def test_resolve_this_friday():
    # From Wed 2026-05-06, "this Friday" = Fri 2026-05-08.
    start, end = resolve_query_date_range("this Friday's plan", TODAY)
    assert start == date(2026, 5, 8)


def test_resolve_month_name_with_year():
    start, end = resolve_query_date_range("May 6, 2026 entry", TODAY)
    assert start == date(2026, 5, 6)
    assert end == date(2026, 5, 6)


def test_resolve_month_name_without_year_uses_today():
    start, end = resolve_query_date_range("the May 6 entry", TODAY)
    assert start == date(2026, 5, 6)


def test_resolve_slash_date_no_year_uses_today():
    start, end = resolve_query_date_range("the 5/6 note", TODAY)
    assert start == date(2026, 5, 6)


def test_resolve_invalid_iso_returns_none_for_phrase():
    # 2026-13-45 cannot resolve; nothing else parses either -> None
    assert resolve_query_date_range("2026-13-45", TODAY) is None


def test_resolve_no_phrase_returns_none():
    assert resolve_query_date_range("what is my favorite color", TODAY) is None
    assert resolve_query_date_range("", TODAY) is None


# --- month_overlaps_range ---

def test_month_overlaps_only_when_range_covers_full_month():
    # full coverage
    assert month_overlaps_range("2026-05-01", date(2026, 5, 1), date(2026, 5, 31))
    # partial coverage -> false
    assert not month_overlaps_range("2026-05-01", date(2026, 5, 6), date(2026, 5, 6))
    assert not month_overlaps_range("2026-05-01", date(2026, 5, 1), date(2026, 5, 30))
    # range extends well beyond the month -> still true (range covers month)
    assert month_overlaps_range("2026-05-01", date(2026, 4, 1), date(2026, 6, 30))


def test_month_overlaps_invalid_iso():
    assert not month_overlaps_range("not-a-date", date(2026, 1, 1), date(2026, 12, 31))


# --- utc_mtime_to_local_date ---

def test_utc_mtime_to_local_date_zero_returns_empty():
    assert utc_mtime_to_local_date(0.0) == ""


def test_utc_mtime_to_local_date_returns_iso_string():
    ts = 1746489600.0  # 2025-05-06 00:00:00 UTC
    result = utc_mtime_to_local_date(ts)
    assert len(result) == 10 and result[4] == "-" and result[7] == "-"
