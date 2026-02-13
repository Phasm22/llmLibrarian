"""
Unit tests for METADATA_ONLY intent routing.
"""
import pytest

from query.intent import route_intent, INTENT_METADATA_ONLY, INTENT_LOOKUP, INTENT_STRUCTURE


def test_metadata_file_counts_by_year():
    """Test metadata intent routing for file counts by year"""
    query = "file counts by year"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_how_many_documents_by_type():
    """Test metadata intent routing for document type counts"""
    query = "how many documents by type"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_extension_breakdown():
    """Test metadata intent routing for extension breakdown"""
    query = "extension breakdown"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_file_counts_by_month():
    """Test metadata intent routing for file counts by month"""
    query = "file counts by month"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_by_quarter():
    """Test metadata intent routing for quarterly counts"""
    query = "how many files by quarter"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_by_folder():
    """Test metadata intent routing for folder counts"""
    query = "count of documents by folder"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_document_types():
    """Test metadata intent routing for document types"""
    query = "document types"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_document_counts():
    """Test metadata intent routing for document counts"""
    query = "document counts"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_metadata_by_extension():
    """Test metadata intent routing for counts by extension"""
    query = "how many files by extension"
    assert route_intent(query) == INTENT_METADATA_ONLY


def test_generic_count_not_metadata():
    """Test that generic 'how many' doesn't trigger metadata intent"""
    query = "how many projects"
    # Should route to PROJECT_COUNT or another intent, not METADATA_ONLY
    assert route_intent(query) != INTENT_METADATA_ONLY


def test_structure_ext_count_not_metadata():
    """Test that specific extension counts route to STRUCTURE, not METADATA_ONLY"""
    query = "how many .docx files"
    # This should route to STRUCTURE (ext-count submode), not METADATA_ONLY
    assert route_intent(query) == INTENT_STRUCTURE


def test_metadata_case_insensitive():
    """Test that metadata routing is case insensitive"""
    query = "FILE COUNTS BY YEAR"
    assert route_intent(query) == INTENT_METADATA_ONLY
