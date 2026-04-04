"""Tests for query-time index error recording (llmli_query_health.json)."""
import json
from pathlib import Path

import pytest

from state import record_index_error, get_query_health, _QUERY_HEALTH_MAX


class _FakeChromaError(Exception):
    pass


def test_record_index_error_writes_entry(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    exc = _FakeChromaError("Error finding id: abc123")
    record_index_error(db, "my-silo-abc", exc)

    entries = get_query_health(db)
    assert len(entries) == 1
    e = entries[0]
    assert e["silo"] == "my-silo-abc"
    assert "_FakeChromaError" in e["error"]
    assert "Error finding id" in e["error"]
    assert e["type"] == "index_corruption"
    assert e["time"]  # non-empty ISO timestamp


def test_get_query_health_empty_when_no_file(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    assert get_query_health(db) == []


def test_record_index_error_appends(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    record_index_error(db, "silo-a", _FakeChromaError("first"))
    record_index_error(db, "silo-b", _FakeChromaError("second"))

    entries = get_query_health(db)
    assert len(entries) == 2
    assert entries[0]["silo"] == "silo-a"
    assert entries[1]["silo"] == "silo-b"


def test_record_index_error_caps_at_max(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    for i in range(_QUERY_HEALTH_MAX + 10):
        record_index_error(db, f"silo-{i}", _FakeChromaError("boom"))

    entries = get_query_health(db)
    assert len(entries) == _QUERY_HEALTH_MAX
    # Oldest entries dropped — last entry should be silo-109 (index 109)
    assert entries[-1]["silo"] == f"silo-{_QUERY_HEALTH_MAX + 9}"


def test_record_index_error_none_slug(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    record_index_error(db, None, _FakeChromaError("no silo"))
    entries = get_query_health(db)
    assert len(entries) == 1
    assert entries[0]["silo"] == ""


def test_record_index_error_silent_on_unwritable_path(tmp_path):
    # Should not raise even if db_path doesn't exist and can't be created.
    # Use a path under a non-existent grandparent that os can't create.
    bad_path = Path("/nonexistent_root_xyz/db")
    # Should not raise
    record_index_error(bad_path, "silo", _FakeChromaError("boom"))
