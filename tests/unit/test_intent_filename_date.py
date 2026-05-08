import pytest

from query.intent import (
    INTENT_EVIDENCE_PROFILE,
    INTENT_FILE_LIST,
    INTENT_FILENAME_DATE_LOOKUP,
    INTENT_LOOKUP,
    INTENT_STRUCTURE,
    route_intent,
)


@pytest.mark.parametrize(
    "query",
    [
        "today's journal entry",
        "give me yesterday's notes",
        "the entry from 2026-05-06",
        "this week's logs",
        "last Monday's note",
        "May 6, 2026 entry",
        "tomorrow's plan file",
    ],
)
def test_routes_to_filename_date_lookup(query):
    assert route_intent(query) == INTENT_FILENAME_DATE_LOOKUP


@pytest.mark.parametrize(
    "query",
    [
        "yesterday i was thinking about taxes",
        "what did i write yesterday about my project",
        "what did i think about taxes yesterday",
        "what did i feel about the meeting today",
        "what i said yesterday regarding the deal",
    ],
)
def test_negative_content_verbs_block_routing(query):
    """Date-phrase + content-verb means the user wants content, not a file."""
    intent = route_intent(query)
    assert intent != INTENT_FILENAME_DATE_LOOKUP


def test_files_from_2022_stays_with_file_list():
    # Bare year is FILE_LIST's domain; the new intent must not steal it.
    assert route_intent("list files from 2022") == INTENT_FILE_LIST


def test_recent_files_phrasing_does_not_route_to_filename_date():
    # Whatever else they route to, they must not be stolen by FILENAME_DATE_LOOKUP.
    assert route_intent("what changed recently") != INTENT_FILENAME_DATE_LOOKUP
    assert route_intent("show me recent files") != INTENT_FILENAME_DATE_LOOKUP


def test_no_date_phrase_falls_through():
    assert route_intent("what is my favorite color") != INTENT_FILENAME_DATE_LOOKUP


def test_date_phrase_without_file_noun_does_not_trigger():
    # "today" alone is too vague — let LOOKUP handle and content-rank.
    assert route_intent("today") != INTENT_FILENAME_DATE_LOOKUP


def test_overlong_query_falls_through():
    long_q = "today's entry and also tell me everything you know about my finances and plans"
    assert route_intent(long_q) != INTENT_FILENAME_DATE_LOOKUP


def test_iso_date_with_file_noun_short_query():
    assert route_intent("2026-05-06 note") == INTENT_FILENAME_DATE_LOOKUP


def test_evidence_profile_self_question_unaffected():
    # No date phrase here, classic EVIDENCE_PROFILE.
    assert route_intent("what do i like about minimalism") == INTENT_EVIDENCE_PROFILE
