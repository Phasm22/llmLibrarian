"""
Unit tests for temporal query decomposition.
"""
import pytest

from query.expansion import decompose_temporal_query


def test_year_range_from_to():
    """Test year range pattern: 'from YYYY to YYYY'"""
    query = "How did revenue change from 2023 to 2024"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "2023" in result[0]
    assert "2024" in result[1]
    assert "revenue" in result[0].lower()
    assert "revenue" in result[1].lower()


def test_year_comparison_vs():
    """Test year comparison pattern: 'YYYY vs YYYY'"""
    query = "Compare architecture 2022 vs 2023"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "2022" in result[0]
    assert "2023" in result[1]
    assert "architecture" in result[0].lower()
    assert "architecture" in result[1].lower()


def test_year_comparison_versus():
    """Test year comparison pattern: 'YYYY versus YYYY'"""
    query = "Performance 2021 versus 2022"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "2021" in result[0]
    assert "2022" in result[1]


def test_month_range():
    """Test month range pattern: 'between Month and Month YYYY'"""
    query = "What happened between January and March 2024"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "January" in result[0] or "january" in result[0].lower()
    assert "March" in result[1] or "march" in result[1].lower()
    assert "2024" in result[0] or "2024" in result[1]


def test_multi_part_question():
    """Test multi-part question pattern: 'What is X and how does Y work'"""
    query = "What is the revenue model and how does pricing work"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "revenue model" in result[0].lower()
    assert "pricing" in result[1].lower()


def test_single_temporal_reference_no_decomposition():
    """Test that single temporal reference doesn't decompose"""
    query = "What was the revenue in 2024"
    result = decompose_temporal_query(query)
    # Should return None because it's not a comparison
    assert result is None


def test_no_temporal_reference_no_decomposition():
    """Test that queries without temporal references don't decompose"""
    query = "What is the company's revenue model"
    result = decompose_temporal_query(query)
    assert result is None


def test_empty_query():
    """Test empty query handling"""
    result = decompose_temporal_query("")
    assert result is None


def test_none_query():
    """Test None query handling"""
    result = decompose_temporal_query(None)
    assert result is None


def test_multi_part_without_question_words():
    """Test that 'and' without question words doesn't decompose"""
    query = "Revenue and expenses report"
    result = decompose_temporal_query(query)
    # Should return None because both parts don't have question words
    assert result is None


def test_year_range_without_subject():
    """Test year range without explicit subject"""
    query = "from 2020 to 2021"
    result = decompose_temporal_query(query)
    assert result is not None
    assert len(result) == 2
    assert "2020" in result[0]
    assert "2021" in result[1]
