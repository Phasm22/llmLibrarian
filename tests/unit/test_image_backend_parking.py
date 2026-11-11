"""Image-backend parking, retry cap, failure dedupe, and orphan resolve.

Covers the contract from the image-backend failure loop: a missing backend parks
images without retries or the write lock; a restored backend re-queues them; a
file-level failure is capped at WATCH_RETRY_CAP attempts; one attempt counts once
in the ledger; deleted files resolve; /healthz gains an additive image_backend field.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import activity_ledger as al
import image_embeddings
import pal
from state import get_last_failures


# ---------------------------------------------------------------------------
# Shared capability (image_embeddings.image_backend_status)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_backend_cache(monkeypatch):
    monkeypatch.setattr(image_embeddings, "_IMAGE_BACKEND_CHECK", None)


def test_backend_status_is_cached_between_checks(monkeypatch, fresh_backend_cache):
    calls = []

    def _probe():
        calls.append(1)
        return "open_clip"

    monkeypatch.setattr(image_embeddings, "image_embedding_backend_name", _probe)
    first = image_embeddings.image_backend_status(now=1000.0)
    again = image_embeddings.image_backend_status(now=1000.0 + 10)
    assert first["status"] == again["status"] == "ok"
    assert len(calls) == 1  # cached: not per file
    image_embeddings.image_backend_status(now=1000.0 + 301)
    assert len(calls) == 2  # re-checked after the timer window


def test_backend_status_reports_missing_with_reason(monkeypatch, fresh_backend_cache):
    monkeypatch.setattr(image_embeddings, "image_embedding_backend_name", lambda: None)
    monkeypatch.setattr(
        image_embeddings, "image_embedding_unavailable_reason", lambda: "ModuleNotFoundError: open_clip"
    )
    status = image_embeddings.image_backend_status(now=5.0)
    assert status["status"] == "missing"
    assert status["reason"] == "ModuleNotFoundError: open_clip"


# ---------------------------------------------------------------------------
# Ingest gating: missing backend parks before any lock or manifest work
# ---------------------------------------------------------------------------


def test_update_single_file_image_with_missing_backend_is_unavailable_not_error(monkeypatch, tmp_path):
    import ingest

    db = tmp_path / "db"
    db.mkdir()
    photo = tmp_path / "photos" / "a.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"not really a jpeg")

    monkeypatch.setattr(ingest, "image_backend_status", lambda **_kw: {"status": "missing", "reason": "x", "checked_at": 0})

    def _no_writer(*_a, **_kw):
        raise AssertionError("missing backend must not open the writer (no lock, no index work)")

    monkeypatch.setattr(ingest, "writer_client", _no_writer)
    recorded = []
    monkeypatch.setattr(ingest, "_record_single_file_failure", lambda *a, **k: recorded.append(a))

    status, resolved = ingest.update_single_file(
        photo, db_path=db, silo_slug="photos-1", allow_cloud=True, image_vision_enabled=False
    )
    assert status == "unavailable"
    assert Path(resolved) == photo.resolve()
    assert recorded == []  # parking is not a failure row; the watcher records it once


def test_update_single_file_non_image_ignores_backend_state(monkeypatch, tmp_path):
    import ingest

    db = tmp_path / "db"
    db.mkdir()
    doc = tmp_path / "notes.md"
    doc.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(ingest, "image_backend_status", lambda **_kw: {"status": "missing", "reason": "x", "checked_at": 0})
    status, _ = ingest.update_single_file(doc, db_path=db, silo_slug="docs-1", allow_cloud=True)
    assert status != "unavailable"


# ---------------------------------------------------------------------------
# MCP update_file / remove_file responses
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    return mcp_server


def _patch_state(monkeypatch, *, slug, silos):
    import state as real_state

    fake_state = SimpleNamespace(
        list_silos=lambda _db: silos,
        resolve_silo_to_slug=lambda _db, _name: slug,
        append_last_failures=real_state.append_last_failures,
        get_last_failures=real_state.get_last_failures,
        failures_path=real_state.failures_path,
    )
    monkeypatch.setitem(sys.modules, "state", fake_state)


def test_update_file_missing_backend_returns_reason_without_lock(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "photos"
    silo_root.mkdir()
    target = silo_root / "a.png"
    target.write_bytes(b"x")
    _patch_state(monkeypatch, slug="photos-1", silos=[{"slug": "photos-1", "path": str(silo_root)}])
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_kw: {"status": "missing", "reason": "x", "checked_at": 0})

    from contextlib import contextmanager

    @contextmanager
    def _no_lock(*_a, **_kw):
        raise AssertionError("missing image backend must not take the write lock")
        yield  # pragma: no cover

    monkeypatch.setattr(mcp_module, "_mcp_chroma_lock", _no_lock)

    res = mcp_module.update_file("photos", str(target), confirm=True)
    assert res["status"] == "error"
    assert res["reason"] == "image_backend_unavailable"
    assert res["retryable"] is False
    assert "uv sync --extra image" in res["message"]
    assert str(silo_root) not in json.dumps(res)


def test_update_file_unavailable_from_ingest_maps_to_same_reason(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "photos"
    silo_root.mkdir()
    target = silo_root / "a.jpg"
    target.write_bytes(b"x")
    _patch_state(monkeypatch, slug="photos-1", silos=[{"slug": "photos-1", "path": str(silo_root)}])
    # Backend looks fine to the cheap pre-check, then ingest reports it parked (race).
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_kw: {"status": "ok", "reason": None, "checked_at": 0})
    _patch_ingest(monkeypatch, update=lambda *a, **kw: ("unavailable", str(a[0])))

    res = mcp_module.update_file("photos", str(target), confirm=True)
    assert res["reason"] == "image_backend_unavailable"
    assert res["retryable"] is False


def _patch_ingest(monkeypatch, *, update=None, remove=None):
    fake = SimpleNamespace(
        update_single_file=update or (lambda *a, **kw: ("updated", str(a[0]))),
        remove_single_file=remove or (lambda *a, **kw: ("removed", str(a[0]))),
    )
    monkeypatch.setitem(sys.modules, "ingest", fake)


def test_update_file_generic_exception_leaks_no_path_or_traceback(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "docs"
    silo_root.mkdir()
    target = silo_root / "secret-name.md"
    target.write_text("x", encoding="utf-8")
    _patch_state(monkeypatch, slug="docs-1", silos=[{"slug": "docs-1", "path": str(silo_root)}])
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_kw: {"status": "ok", "reason": None, "checked_at": 0})

    def _boom(path, **_kw):
        raise OSError(f"cannot read {path}")

    _patch_ingest(monkeypatch, update=_boom)
    res = mcp_module.update_file("docs", str(target), confirm=True)
    text = json.dumps(res)
    assert res["status"] == "error"
    assert res["error"] == "OSError"
    assert str(silo_root) not in text
    assert "Traceback" not in text
    assert "secret-name" not in text


def test_update_file_path_outside_silo_has_no_absolute_path(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    _patch_state(monkeypatch, slug="docs-1", silos=[{"slug": "docs-1", "path": str(silo_root)}])
    res = mcp_module.update_file("docs", str(tmp_path / "outside.txt"), confirm=True)
    assert res["status"] == "error"
    assert str(tmp_path) not in json.dumps(res)


def test_remove_file_generic_exception_leaks_no_path(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "docs"
    silo_root.mkdir()
    target = silo_root / "gone.md"
    _patch_state(monkeypatch, slug="docs-1", silos=[{"slug": "docs-1", "path": str(silo_root)}])

    def _boom(path, **_kw):
        raise OSError(f"cannot remove {path}")

    _patch_ingest(monkeypatch, remove=_boom)
    res = mcp_module.remove_file("docs", str(target), confirm=True)
    assert res["status"] == "error"
    assert str(silo_root) not in json.dumps(res)


def test_healthz_image_backend_field_is_additive(mcp_module, monkeypatch, tmp_path):
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_kw: {"status": "missing", "reason": "x", "checked_at": 0})
    request = Request({"type": "http", "method": "GET", "path": "/healthz", "headers": []})

    async def _run():
        response = await mcp_module.healthz(request)
        body = json.loads(response.body)
        assert body["image_backend"] == "missing"
        # Existing fields are still present and unchanged in meaning.
        assert body["ok"] is True
        assert body["service"] == "llmLibrarian-mcp"
        assert "db_exists" in body and "started_at" in body and "version" in body

    asyncio.run(_run())


def test_health_state_degrades_on_missing_and_recovers(monkeypatch, mcp_module):
    out = io.StringIO()
    health = mcp_module.HealthState(enabled=True, out=out)
    monkeypatch.setattr(mcp_module, "_HEALTH", health)

    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **kw: {"status": "missing", "reason": "x", "checked_at": 0})
    mcp_module._refresh_image_backend_health()
    assert health.status == "degraded"
    assert health.reason == mcp_module._HEALTH_IMAGE_BACKEND_MISSING

    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **kw: {"status": "ok", "reason": None, "checked_at": 0})
    mcp_module._refresh_image_backend_health()
    assert health.status == "recovered"

    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["status"] for e in events] == ["degraded", "recovered"]


# ---------------------------------------------------------------------------
# Activity ledger: classes, dedupe on (path, mtime), resolve, orphan prune
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_ACTIVITY", "1")
    db = tmp_path / "db"
    db.mkdir()
    root = tmp_path / "photos"
    root.mkdir()
    return db, root


def _rows(db, sql, args=()):
    conn = sqlite3.connect(str(al.ledger_path(db)))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_error_class_names_the_actionable_causes():
    assert al.error_class("image embeddings unavailable; run `uv sync --extra image`") == "image_backend"
    assert al.error_class("image model not ready") == "image_model"
    assert al.error_class("zip processing failed") == "index_error"
    assert al.error_class("file could not be indexed; see the server log") == "index_error"


def test_same_file_version_counts_once_across_reporters(ledger_db):
    db, root = ledger_db
    photo = str(root / "a.jpg")
    al.record_failures(db, [{"path": photo, "error": "image embeddings unavailable", "mtime": 100.0}])
    # The MCP server and the watcher both report the same attempt.
    al.record_failures(db, [{"path": photo, "error": "image_backend_unavailable", "mtime": 100.0}])
    assert _rows(db, "SELECT occurrences, error_class FROM failures WHERE path=?", (photo,)) == [(1, "image_backend")]


def test_new_file_version_is_a_new_occurrence(ledger_db):
    db, root = ledger_db
    photo = str(root / "a.jpg")
    al.record_failures(db, [{"path": photo, "error": "image model not ready", "mtime": 100.0}])
    al.record_failures(db, [{"path": photo, "error": "image model not ready", "mtime": 200.0}])
    assert _rows(db, "SELECT occurrences FROM failures WHERE path=?", (photo,)) == [(2,)]


def test_removed_file_resolves_its_failure(ledger_db, monkeypatch):
    db, root = ledger_db
    monkeypatch.setattr(al, "_silo_roots", lambda _db: {"photos-1": str(root)})
    photo = str(root / "gone.jpg")
    al.record_failures(db, [{"path": photo, "error": "image model not ready", "mtime": 1.0}])
    al.resolve_failures(db, "photos-1", photo)
    assert _rows(db, "SELECT resolved_at IS NOT NULL FROM failures WHERE path=?", (photo,)) == [(1,)]


def test_orphan_prune_guards_on_the_silo_root_not_the_parent(ledger_db, tmp_path):
    db, root = ledger_db
    live = root / "still-here.jpg"
    live.write_bytes(b"x")
    # Whole subdirectory deleted under a mounted root: the deletion is real.
    gone_dir_file = root / "vampire-portraits" / "render-1.png"
    # File deleted in the root itself: also real.
    gone_file = root / "deleted.jpg"
    # Silo root unmounted (disconnected volume): must never resolve.
    offline_root = tmp_path / "unmounted-volume"
    offline_file = offline_root / "x.jpg"
    roots = {"photos-1": str(root), "offline-1": str(offline_root)}
    al.record_failures(
        db,
        [
            {"path": str(live), "error": "image model not ready", "mtime": 1.0},
            {"path": str(gone_dir_file), "error": "image model not ready", "mtime": 1.0},
            {"path": str(gone_file), "error": "image model not ready", "mtime": 1.0},
        ],
    )
    # Attribute the rows to their silo roots explicitly (the registry is not populated here).
    conn = sqlite3.connect(str(al.ledger_path(db)), isolation_level=None)
    conn.execute("UPDATE failures SET silo='photos-1'")
    conn.execute(
        "INSERT INTO failures(silo, path, error_class, error, first_seen, last_seen, file_mtime)"
        " VALUES ('offline-1', ?, 'image_model', 'x', 1, 1, 1)",
        (str(offline_file),),
    )
    try:
        assert al.prune_orphan_failures(conn, roots=roots) == 2
    finally:
        conn.close()
    open_paths = {p for (p,) in _rows(db, "SELECT path FROM failures WHERE resolved_at IS NULL")}
    assert open_paths == {str(live), str(offline_file)}


def test_orphan_prune_unregistered_silo_falls_back_to_parent_directory(ledger_db, tmp_path):
    db, root = ledger_db
    deleted = root / "deleted.jpg"
    missing_dir_file = tmp_path / "vanished-dir" / "x.jpg"
    al.record_failures(
        db,
        [
            {"path": str(deleted), "error": "image model not ready", "mtime": 1.0},
            {"path": str(missing_dir_file), "error": "image model not ready", "mtime": 1.0},
        ],
    )
    conn = sqlite3.connect(str(al.ledger_path(db)), isolation_level=None)
    try:
        # No roots: "unknown" silo, so the guard is the immediate parent directory.
        assert al.prune_orphan_failures(conn, roots={}) == 1
    finally:
        conn.close()
    open_paths = {p for (p,) in _rows(db, "SELECT path FROM failures WHERE resolved_at IS NULL")}
    assert open_paths == {str(missing_dir_file)}


def test_orphan_prune_migration_runs_once_and_adds_mtime_column(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_ACTIVITY", "1")
    db = tmp_path / "db"
    db.mkdir()
    ledger = al.ledger_path(db)
    # A pre-upgrade ledger: failures table without file_mtime, with an orphan open row.
    old = sqlite3.connect(str(ledger))
    old.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE failures (
            id INTEGER PRIMARY KEY, silo TEXT NOT NULL, path TEXT NOT NULL,
            error_class TEXT NOT NULL, error TEXT NOT NULL, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1, resolved_at REAL
        );
        """
    )
    old.execute(
        "INSERT INTO failures(silo, path, error_class, error, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
        ("photos-1", str(tmp_path / "vanished.jpg"), "other", "boom", 1.0, 1.0),
    )
    old.commit()
    old.close()

    monkeypatch.setattr(al, "_INITIALIZED", set())
    conn = al._connect(db)
    conn.close()
    cols = {r[1] for r in _rows(db, "PRAGMA table_info(failures)")}
    assert "file_mtime" in cols
    assert _rows(db, "SELECT resolved_at IS NOT NULL FROM failures") == [(1,)]

    # Second open: the flag makes the prune a no-op, so a later real failure stays open.
    later = tmp_path / "later.jpg"
    al.record_failures(db, [{"path": str(later), "error": "image model not ready", "mtime": 1.0}])
    monkeypatch.setattr(al, "_INITIALIZED", set())
    al._connect(db).close()
    assert _rows(db, "SELECT resolved_at IS NULL FROM failures WHERE path=?", (str(later),)) == [(1,)]


# ---------------------------------------------------------------------------
# pal watcher: park, cap, un-park on new mtime, backend-restore re-queue
# ---------------------------------------------------------------------------


class _DummyObserver:
    def schedule(self, *a, **k):
        return None

    def start(self):
        return None

    def stop(self):
        return None

    def join(self):
        return None


def _watcher(monkeypatch, root: Path):
    monkeypatch.setattr(pal, "Observer", _DummyObserver)
    monkeypatch.setattr(pal, "PAL_HOME", root / ".pal")
    watcher = pal.SiloWatcher(root, db_path=str(root / "db"), interval=10, debounce=1)
    watcher._log = lambda message: None
    return watcher


def _photo(root: Path, name: str = "a.jpg") -> Path:
    root.mkdir(exist_ok=True)
    p = root / name
    p.write_bytes(b"x")
    return p


def test_unavailable_image_is_parked_not_requeued(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    calls = []

    def _mcp(tool, **kw):
        calls.append(tool)
        return {"status": "error", "reason": "image_backend_unavailable", "message": "unavailable", "retryable": False}

    monkeypatch.setattr(pal, "_mcp_call_sync", _mcp)
    now = pal.time.time()
    watcher.enqueue_update(str(photo))
    watcher._drain_due(now=now + 10)
    watcher._drain_due(now=now + 10_000)

    assert calls == ["update_file"]  # one attempt, no retry loop
    assert str(photo.resolve()) not in watcher._queue
    assert watcher._parked[str(photo.resolve())]["reason"] == pal.PARK_IMAGE_BACKEND


def test_parked_file_ignores_same_mtime_watch_events(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    monkeypatch.setattr(
        pal, "_mcp_call_sync",
        lambda tool, **kw: {"status": "error", "reason": "image_backend_unavailable", "retryable": False},
    )
    watcher.enqueue_update(str(photo))
    watcher._drain_due(now=pal.time.time() + 10)
    watcher.enqueue_update(str(photo))  # editor touch, same mtime
    assert str(photo.resolve()) not in watcher._queue


def test_parked_file_is_released_by_a_new_mtime(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    monkeypatch.setattr(
        pal, "_mcp_call_sync",
        lambda tool, **kw: {"status": "error", "reason": "image_backend_unavailable", "retryable": False},
    )
    watcher.enqueue_update(str(photo))
    watcher._drain_due(now=pal.time.time() + 10)
    os.utime(photo, (photo.stat().st_atime + 50, photo.stat().st_mtime + 50))
    watcher.enqueue_update(str(photo))
    assert str(photo.resolve()) in watcher._queue
    assert str(photo.resolve()) not in watcher._parked


def test_backend_restored_requeues_parked_images(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    state = {"tool_response": {"status": "error", "reason": "image_backend_unavailable", "retryable": False}}
    monkeypatch.setattr(pal, "_mcp_call_sync", lambda tool, **kw: dict(state["tool_response"]))
    now = pal.time.time()
    watcher.enqueue_update(str(photo))
    watcher._drain_due(now=now + 10)
    assert str(photo.resolve()) in watcher._parked

    # Backend still missing at the next poll: stays parked.
    monkeypatch.setattr(pal, "_mcp_image_backend_state", lambda: "missing")
    watcher._drain_due(now=now + 10 + pal.IMAGE_BACKEND_POLL_SECONDS + 1)
    assert str(photo.resolve()) in watcher._parked

    # Backend back: re-queued by the poll, then indexed on the next drain.
    monkeypatch.setattr(pal, "_mcp_image_backend_state", lambda: "ok")
    state["tool_response"] = {"status": "updated", "silo": "repo", "path": str(photo.resolve())}
    later = now + 10 + 2 * pal.IMAGE_BACKEND_POLL_SECONDS + 2
    watcher._drain_due(now=later)
    assert str(photo.resolve()) not in watcher._parked
    assert str(photo.resolve()) in watcher._queue
    watcher._drain_due(now=later + 1)
    assert str(photo.resolve()) not in watcher._queue
    assert str(photo.resolve()) not in watcher._parked


def test_parked_images_keep_the_worker_polling_with_an_empty_queue(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    monkeypatch.setattr(
        pal, "_mcp_call_sync",
        lambda tool, **kw: {"status": "error", "reason": "image_backend_unavailable", "retryable": False},
    )
    watcher.enqueue_update(str(photo))
    watcher._drain_due(now=pal.time.time() + 10)
    assert watcher._queue == {}
    assert watcher._seconds_until_next_due() == pal.IMAGE_BACKEND_POLL_SECONDS


def test_retry_is_capped_then_parked_until_the_file_changes(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    doc = root / "doc.md"
    _photo(root, "doc.md")
    watcher = _watcher(monkeypatch, root)
    attempts = []

    def _mcp(tool, **kw):
        attempts.append(tool)
        return {"status": "error", "reason": "index_error", "message": "could not be indexed", "retryable": True}

    monkeypatch.setattr(pal, "_mcp_call_sync", _mcp)
    now = pal.time.time()
    watcher.enqueue_update(str(doc))
    for i in range(1, pal.WATCH_RETRY_CAP + 3):
        watcher._drain_due(now=now + 10_000 * i)
    assert len(attempts) == pal.WATCH_RETRY_CAP
    assert watcher._parked[str(doc.resolve())]["reason"] == "retry_exhausted"


def test_transport_outage_is_retried_without_cap_or_parking(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    photo = _photo(root)
    watcher = _watcher(monkeypatch, root)
    recorded = []
    import state

    monkeypatch.setattr(state, "append_last_failures", lambda *a, **k: recorded.append(a))

    def _down(tool, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(pal, "_mcp_call_sync", _down)
    now = pal.time.time()
    watcher.enqueue_update(str(photo))
    for i in range(1, pal.WATCH_RETRY_CAP + 3):
        watcher._drain_due(now=now + 10_000 * i)
    assert watcher._parked == {}
    assert recorded == []  # an MCP outage is not a file failure
    assert str(photo.resolve()) in watcher._queue
