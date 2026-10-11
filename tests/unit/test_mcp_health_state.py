"""service.state events: one stdout line per health transition, never on a timer."""
from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

import mcp_server
from mcp_server import HealthState

STATUSES = {"ready", "degraded", "recovered", "stopping", "error"}


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


def _assert_contract(event: dict) -> None:
    assert event["event"] == "service.state"
    assert event["status"] in STATUSES
    assert isinstance(event["reason"], str) and event["reason"]
    assert isinstance(event["metrics"], dict)
    ts = datetime.fromisoformat(event["ts"])
    assert ts.utcoffset().total_seconds() == 0
    assert ts.microsecond == 0


def test_transitions_emit_once_per_change():
    buf = io.StringIO()
    health = HealthState(enabled=True, out=buf)

    assert health.set("ready", "listening; db present", db_exists=True)
    assert health.set("degraded", "db missing", db_exists=False)
    assert not health.set("degraded", "db missing", db_exists=False)
    assert health.recover("db missing", "db present", db_exists=True)

    events = _lines(buf)
    assert len(events) == 3
    for event in events:
        _assert_contract(event)
    assert [e["status"] for e in events] == ["ready", "degraded", "recovered"]
    assert events[1]["metrics"] == {"db_exists": False}


def test_reason_change_alone_emits():
    buf = io.StringIO()
    health = HealthState(enabled=True, out=buf)
    health.set("degraded", "db missing")
    health.set("degraded", "chroma busy")
    assert [e["reason"] for e in _lines(buf)] == ["db missing", "chroma busy"]


def test_recover_only_clears_its_own_degradation():
    buf = io.StringIO()
    health = HealthState(enabled=True, out=buf)
    health.set("degraded", "db missing")
    assert not health.recover("chroma busy", "queries succeeding")
    assert health.status == "degraded"
    assert len(_lines(buf)) == 1


def test_disabled_tracks_state_but_prints_nothing():
    buf = io.StringIO()
    health = HealthState(out=buf)
    assert not health.set("ready", "listening; db present")
    assert health.status == "ready"
    assert buf.getvalue() == ""


def test_emit_failure_is_swallowed():
    class Broken(io.StringIO):
        def write(self, _s):
            raise OSError("broken pipe")

    health = HealthState(enabled=True, out=Broken())
    assert health.set("ready", "listening; db present") is False


@pytest.fixture
def health(monkeypatch, tmp_path):
    buf = io.StringIO()
    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_HEALTH", HealthState(enabled=True, out=buf))
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_db_present_last", None)
    return buf, db


def test_startup_ready_then_db_transitions(health):
    buf, db = health
    mcp_server._health_startup()
    mcp_server._db_present()  # unchanged: no line
    db.rmdir()
    assert mcp_server._db_present() is False
    assert mcp_server._db_present() is False
    db.mkdir()
    assert mcp_server._db_present() is True

    events = _lines(buf)
    assert [(e["status"], e["reason"]) for e in events] == [
        ("ready", "listening; db present"),
        ("degraded", "db missing"),
        ("recovered", "db present"),
    ]


def test_startup_without_db_is_degraded(health):
    buf, db = health
    db.rmdir()
    mcp_server._health_startup()
    events = _lines(buf)
    assert len(events) == 1
    assert events[0]["status"] == "degraded"
    assert events[0]["metrics"] == {"db_exists": False}


def test_lock_busy_degrades_until_next_successful_chroma_use(health, monkeypatch):
    buf, _db = health
    monkeypatch.setattr(mcp_server, "_mcp_read_lock_disabled", lambda: False)
    mcp_server._health_startup()

    mcp_server._busy_error(TimeoutError("held"), "query_personal_knowledge")
    mcp_server._busy_error(TimeoutError("held"), "find_files")
    with pytest.raises(TimeoutError):
        with mcp_server._mcp_chroma_lock("query_personal_knowledge"):
            raise TimeoutError("still held")
    assert mcp_server._HEALTH.status == "degraded"

    with mcp_server._mcp_chroma_lock("query_personal_knowledge"):
        pass

    assert [e["status"] for e in _lines(buf)] == ["ready", "degraded", "recovered"]


def test_healthz_reports_db_transition(health):
    import asyncio

    from starlette.requests import Request

    buf, db = health
    mcp_server._health_startup()
    db.rmdir()
    request = Request({"type": "http", "method": "GET", "path": "/healthz", "headers": []})
    body = json.loads(asyncio.run(mcp_server.healthz(request)).body)
    assert body["db_exists"] is False
    assert [e["status"] for e in _lines(buf)] == ["ready", "degraded"]
