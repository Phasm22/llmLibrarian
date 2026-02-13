"""
Unit tests for TIMELINE intent routing.
"""
import pytest

from query.intent import route_intent, INTENT_TIMELINE, INTENT_LOOKUP


def test_timeline_with_year_range():
    """Test timeline intent routing with year range"""
    query = "timeline of project milestones 2023-2024"
    assert route_intent(query) == INTENT_TIMELINE


def test_chronological_with_events():
    """Test chronological keyword triggers timeline"""
    query = "chronological history of events"
    assert route_intent(query) == INTENT_TIMELINE


def test_sequence_with_changes():
    """Test sequence keyword triggers timeline"""
    query = "sequence of changes in 2024"
    assert route_intent(query) == INTENT_TIMELINE


def test_history_with_milestones():
    """Test history keyword triggers timeline"""
    query = "history of milestones"
    assert route_intent(query) == INTENT_TIMELINE


def test_evolution_with_updates():
    """Test evolution keyword triggers timeline"""
    query = "evolution of updates"
    assert route_intent(query) == INTENT_TIMELINE


def test_progression_with_year():
    """Test progression keyword triggers timeline"""
    query = "progression of events in 2023"
    assert route_intent(query) == INTENT_TIMELINE


def test_timeline_without_temporal_context():
    """Test that timeline without temporal context doesn't trigger"""
    query = "timeline information"
    # Should not trigger because no year/events/milestones/changes
    assert route_intent(query) != INTENT_TIMELINE


def test_generic_query_not_timeline():
    """Test that generic queries don't trigger timeline"""
    query = "What is the revenue"
    assert route_intent(query) == INTENT_LOOKUP


def test_timeline_case_insensitive():
    """Test that timeline routing is case insensitive"""
    query = "TIMELINE OF EVENTS 2024"
    assert route_intent(query) == INTENT_TIMELINE
