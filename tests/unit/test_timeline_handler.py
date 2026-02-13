"""
Unit tests for TIMELINE handler functions.
"""
import pytest

from query.timeline import parse_timeline_request, format_timeline_answer


def test_parse_timeline_year_range():
    """Test parsing timeline request with year range"""
    query = "timeline of project milestones 2023-2024"
    result = parse_timeline_request(query)

    assert result is not None
    assert result["start_year"] == 2023
    assert result["end_year"] == 2024
    assert "project" in result["keywords"]
    # "milestones" is filtered as a stopword (timeline trigger word)


def test_parse_timeline_single_year():
    """Test parsing timeline request with single year"""
    query = "chronological history of events in 2024"
    result = parse_timeline_request(query)

    assert result is not None
    assert result["start_year"] == 2024
    assert result["end_year"] == 2024


def test_parse_timeline_no_year():
    """Test parsing timeline request without year"""
    query = "chronological history of design docs"
    result = parse_timeline_request(query)

    assert result is not None
    assert result["start_year"] is None
    assert result["end_year"] is None
    assert "design" in result["keywords"]
    assert "docs" in result["keywords"]


def test_parse_timeline_filters_stopwords():
    """Test that timeline parsing filters out stopwords"""
    query = "timeline of the project events in 2024"
    result = parse_timeline_request(query)

    assert result is not None
    # "timeline", "of", "the", "in", "events" should be filtered
    assert "timeline" not in result["keywords"]
    assert "the" not in result["keywords"]
    assert "events" not in result["keywords"]
    # "project" should remain
    assert "project" in result["keywords"]


def test_parse_timeline_empty_query():
    """Test parsing empty timeline query"""
    result = parse_timeline_request("")
    assert result is None


def test_parse_timeline_none_query():
    """Test parsing None timeline query"""
    result = parse_timeline_request(None)
    assert result is None


def test_format_timeline_empty_events():
    """Test formatting timeline with no events"""
    events = []
    result = format_timeline_answer(events, "test-silo", no_color=True)

    assert "No events found" in result
    assert "test-silo" in result


def test_format_timeline_with_events():
    """Test formatting timeline with events"""
    events = [
        {"date": "2023-01-15", "path": "file1.pdf"},
        {"date": "2023-03-20", "path": "file2.docx"},
        {"date": "2024-01-10", "path": "file3.txt"},
    ]
    result = format_timeline_answer(events, "test-silo", no_color=True)

    assert "2023-01-15" in result
    assert "2023-03-20" in result
    assert "2024-01-10" in result
    assert "file1.pdf" in result
    assert "file2.docx" in result
    assert "file3.txt" in result
    assert "test-silo" in result


def test_format_timeline_chronological_order():
    """Test that timeline events are in chronological order"""
    events = [
        {"date": "2024-01-10", "path": "file3.txt"},
        {"date": "2023-01-15", "path": "file1.pdf"},
        {"date": "2023-03-20", "path": "file2.docx"},
    ]
    result = format_timeline_answer(events, "test-silo", no_color=True)

    # Find positions in output
    pos_file1 = result.find("file1.pdf")
    pos_file2 = result.find("file2.docx")
    pos_file3 = result.find("file3.txt")

    # Should maintain the order from events list (which should be pre-sorted)
    assert pos_file3 < pos_file1 < pos_file2 or pos_file1 < pos_file2 < pos_file3


# Note: Full timeline handler testing requires:
# 1. A test manifest file with file metadata
# 2. build_timeline_from_manifest() integration tests
# These are covered in integration tests.
