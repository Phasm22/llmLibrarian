from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pal


@pytest.fixture
def watcher_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(pal, "PAL_HOME", tmp_path / ".pal")
    monkeypatch.setattr(pal, "WATCH_LOCKS_DIR", tmp_path / ".pal" / "watch_locks")
    db = tmp_path / "db"
    db.mkdir()
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")

    emitted: list[dict] = []

    def _capture(logger, event, **fields):
        emitted.append({"event": event, **fields})

    import watch_telemetry as wt

    monkeypatch.setattr(wt, "emit_watch_event", _capture)
    monkeypatch.setattr(pal, "_mcp_call_sync", lambda tool, **kw: {"status": "updated", "silo": "demo", "path": kw.get("path")})

    monkeypatch.setattr(pal, "Observer", MagicMock)
    monkeypatch.setattr(pal, "FileSystemEventHandler", MagicMock)

    def _fake_ensure():
        pass

    monkeypatch.setattr(pal, "_ensure_src_on_path", _fake_ensure)

    # Minimal ingest/state stubs for SiloWatcher.__init__
    fake_ingest = MagicMock()
    fake_ingest.ADD_DEFAULT_INCLUDE = ["*"]
    fake_ingest.ADD_DEFAULT_EXCLUDE = []
    fake_ingest.collect_files.return_value = []
    fake_ingest.should_index.return_value = True
    fake_ingest._read_file_manifest.return_value = {"silos": {}}
    fake_ingest._load_limits_config.return_value = (1_000_000, 10, 1_000_000, 100, 100)
    monkeypatch.setitem(__import__("sys").modules, "ingest", fake_ingest)

    fake_state = MagicMock()
    fake_state.get_silo_exclude_patterns.return_value = []
    fake_state.append_last_failures = MagicMock()
    fake_state.failures_path = lambda db: Path(db) / "llmli_last_failures.json"
    fake_state.get_last_failures = lambda _db: []
    monkeypatch.setitem(__import__("sys").modules, "state", fake_state)

    watcher = pal.SiloWatcher(
        root=root,
        db_path=str(db),
        silo_slug="demo",
        interval=60.0,
        debounce=0.0,
    )
    return watcher, emitted, db


def test_drain_due_emits_batch_drain(watcher_setup):
    watcher, emitted, _db = watcher_setup
    path = str(watcher.root / "a.txt")
    watcher._queue_action(path, "update", delay=0.0)
    with watcher._queue_lock:
        for meta in watcher._queue.values():
            meta["due_at"] = 0.0
    watcher._drain_due(now=time.time())

    batch = [e for e in emitted if e.get("event") == "batch_drain"]
    assert len(batch) == 1
    assert batch[0]["silo"] == "demo"
    assert batch[0]["updated"] == 1
    assert "duration_ms" in batch[0]
    assert batch[0]["last_failures_path"].endswith("llmli_last_failures.json")


def test_drain_due_treats_skipped_as_success(watcher_setup, monkeypatch):
    watcher, emitted, _db = watcher_setup

    monkeypatch.setattr(
        pal,
        "_mcp_call_sync",
        lambda tool, **kw: {"status": "skipped", "silo": "demo", "path": kw.get("path")},
    )

    path = str(watcher.root / "skipme.txt")
    watcher._queue_action(path, "update", delay=0.0)
    with watcher._queue_lock:
        for meta in watcher._queue.values():
            meta["due_at"] = 0.0

    processed = watcher._drain_due(now=time.time())

    assert processed == 0
    assert watcher._queue == {}
    batch = [e for e in emitted if e.get("event") == "batch_drain"]
    assert batch and batch[-1].get("skipped") == 1


def test_reconcile_emits_event_when_queued(watcher_setup, monkeypatch):
    watcher, emitted, _db = watcher_setup

    def _reconcile():
        watcher._queue_action(str(watcher.root / "b.txt"), "update", delay=0.0)
        return (1, 0, 0)

    monkeypatch.setattr(watcher, "_reconcile_once", _reconcile)
    watcher._emit_reconcile_event(1, 0, 0, duration_ms=5)

    recon = [e for e in emitted if e.get("event") == "reconcile"]
    assert len(recon) == 1
    assert recon[0]["queued_updates"] == 1
