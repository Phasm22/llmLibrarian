from __future__ import annotations

import json
import logging

import pytest

from watch_telemetry import EVENT_BATCH_DRAIN, emit_watch_event, watch_json_enabled


def test_emit_watch_event_writes_json_line():
    logger = logging.getLogger("test.watch.telemetry")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(_Capture())
    logger.propagate = False

    emit_watch_event(logger, EVENT_BATCH_DRAIN, silo="docs", updated=2, duration_ms=10)

    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["event"] == EVENT_BATCH_DRAIN
    assert payload["silo"] == "docs"
    assert payload["updated"] == 2
    assert payload["duration_ms"] == 10
    assert "ts" in payload


def test_watch_json_disabled(monkeypatch):
    monkeypatch.setenv("PAL_WATCH_JSON", "0")
    assert watch_json_enabled() is False

    logger = logging.getLogger("test.watch.telemetry.off")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(_Capture())
    logger.propagate = False

    emit_watch_event(logger, EVENT_BATCH_DRAIN, silo="docs")
    assert records == []
