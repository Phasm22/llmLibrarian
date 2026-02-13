"""
Unit tests for METADATA_ONLY handler functions.
"""
import pytest

from query.metadata import parse_metadata_request, format_metadata_answer


def test_parse_metadata_by_year():
    """Test parsing metadata request with 'by year'"""
    query = "file counts by year"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "year"


def test_parse_metadata_by_month():
    """Test parsing metadata request with 'by month'"""
    query = "how many documents by month"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "month"


def test_parse_metadata_by_quarter():
    """Test parsing metadata request with 'by quarter'"""
    query = "file counts by quarter"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "quarter"


def test_parse_metadata_by_extension():
    """Test parsing metadata request with 'by extension'"""
    query = "how many files by extension"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "extension"


def test_parse_metadata_by_type():
    """Test parsing metadata request with 'by type'"""
    query = "document counts by type"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "extension"  # "by type" maps to extension


def test_parse_metadata_extension_breakdown():
    """Test parsing metadata request with 'extension breakdown'"""
    query = "extension breakdown"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "extension"


def test_parse_metadata_document_types():
    """Test parsing metadata request with 'document types'"""
    query = "document types"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "extension"


def test_parse_metadata_by_folder():
    """Test parsing metadata request with 'by folder'"""
    query = "file counts by folder"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "folder"


def test_parse_metadata_default_to_extension():
    """Test that unspecified dimension defaults to extension"""
    query = "file counts"
    result = parse_metadata_request(query)

    assert result is not None
    assert result["dimension"] == "extension"


def test_parse_metadata_empty_query():
    """Test parsing empty metadata query"""
    result = parse_metadata_request("")
    assert result is None


def test_parse_metadata_none_query():
    """Test parsing None metadata query"""
    result = parse_metadata_request(None)
    assert result is None


def test_format_metadata_empty_aggregates():
    """Test formatting metadata with no aggregates"""
    aggregates = []
    result = format_metadata_answer("year", aggregates, "test-silo", no_color=True)

    assert "No metadata found" in result
    assert "test-silo" in result


def test_format_metadata_by_extension():
    """Test formatting metadata aggregates by extension"""
    aggregates = [
        {"label": ".pdf", "count": 42},
        {"label": ".docx", "count": 28},
        {"label": ".txt", "count": 15},
    ]
    result = format_metadata_answer("extension", aggregates, "test-silo", no_color=True)

    assert "File counts by extension" in result
    assert ".pdf: 42 files" in result
    assert ".docx: 28 files" in result
    assert ".txt: 15 files" in result
    assert "test-silo" in result


def test_format_metadata_by_year():
    """Test formatting metadata aggregates by year"""
    aggregates = [
        {"label": "2024", "count": 50},
        {"label": "2023", "count": 35},
        {"label": "2022", "count": 20},
    ]
    result = format_metadata_answer("year", aggregates, "test-silo", no_color=True)

    assert "File counts by year" in result
    assert "2024: 50 files" in result
    assert "2023: 35 files" in result
    assert "2022: 20 files" in result


def test_format_metadata_single_file_singular():
    """Test that singular 'file' is used for count=1"""
    aggregates = [
        {"label": ".pdf", "count": 1},
    ]
    result = format_metadata_answer("extension", aggregates, "test-silo", no_color=True)

    assert ".pdf: 1 file" in result
    assert "files" not in result or "1 file" in result


def test_format_metadata_by_month():
    """Test formatting metadata aggregates by month"""
    aggregates = [
        {"label": "2024-01", "count": 10},
        {"label": "2024-02", "count": 15},
    ]
    result = format_metadata_answer("month", aggregates, "test-silo", no_color=True)

    assert "File counts by month" in result
    assert "2024-01: 10 files" in result
    assert "2024-02: 15 files" in result


def test_format_metadata_by_quarter():
    """Test formatting metadata aggregates by quarter"""
    aggregates = [
        {"label": "2024-Q1", "count": 25},
        {"label": "2024-Q2", "count": 30},
    ]
    result = format_metadata_answer("quarter", aggregates, "test-silo", no_color=True)

    assert "File counts by quarter" in result
    assert "2024-Q1: 25 files" in result
    assert "2024-Q2: 30 files" in result


# Note: Full metadata handler testing requires:
# 1. A test manifest file with file metadata
# 2. aggregate_metadata() integration tests
# These are covered in integration tests.
