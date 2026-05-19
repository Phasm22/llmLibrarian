"""Coverage for the update_file / remove_file MCP tool guard rails.

These tools route the watcher's per-file events through the shared MCP server
so only one PersistentClient ever opens the DB. Tests cover synchronous return
contracts (db missing, confirm gating, silo resolution, path-under-silo
validation, happy-path delegation to ingest helpers).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


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


def _patch_ingest(monkeypatch, *, update=None, remove=None):
    fake = SimpleNamespace(
        update_single_file=update or (lambda *a, **kw: ("updated", str(a[0]))),
        remove_single_file=remove or (lambda *a, **kw: ("removed", str(a[0]))),
    )
    monkeypatch.setitem(sys.modules, "ingest", fake)


# ---------- update_file ----------


def test_update_file_requires_confirm(mcp_module):
    res = mcp_module.update_file("docs", "/x", confirm=False)
    assert res["status"] == "not_started"
    assert "confirm=True" in res["message"]


def test_update_file_db_missing(monkeypatch, tmp_path):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", str(tmp_path / "missing"))
    res = mcp_server.update_file("docs", "/x", confirm=True)
    assert res["status"] == "error"
    assert "does not exist" in res["error"]


def test_update_file_silo_not_found(monkeypatch, mcp_module):
    _patch_state(monkeypatch, slug=None, silos=[])
    res = mcp_module.update_file("ghost", "/tmp/x.txt", confirm=True)
    assert res["status"] == "error"
    assert "silo not found" in res["error"]


def test_update_file_path_outside_silo_root(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    _patch_state(
        monkeypatch,
        slug="docs-1",
        silos=[{"slug": "docs-1", "path": str(silo_root)}],
    )
    elsewhere = tmp_path / "elsewhere" / "x.txt"
    elsewhere.parent.mkdir()
    elsewhere.write_text("hi", encoding="utf-8")

    res = mcp_module.update_file("docs", str(elsewhere), confirm=True)
    assert res["status"] == "error"
    assert "not under silo source" in res["error"]


def test_update_file_happy_path_calls_update_single_file(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    target = silo_root / "doc.md"
    target.write_text("hello", encoding="utf-8")
    _patch_state(
        monkeypatch,
        slug="docs-1",
        silos=[{"slug": "docs-1", "path": str(silo_root)}],
    )
    calls: list[tuple] = []

    def _fake_update(path, db_path, silo_slug, allow_cloud=False):
        calls.append((str(path), str(db_path), silo_slug, allow_cloud))
        return ("updated", str(path))

    _patch_ingest(monkeypatch, update=_fake_update)

    res = mcp_module.update_file("docs", str(target), confirm=True)

    assert res["status"] == "updated"
    assert res["silo"] == "docs-1"
    assert Path(res["path"]) == target.resolve()
    assert len(calls) == 1
    assert calls[0][2] == "docs-1"
    assert calls[0][3] is True  # allow_cloud=True (path already validated)


def test_update_file_propagates_ingest_error(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    target = silo_root / "doc.md"
    target.write_text("hi", encoding="utf-8")
    _patch_state(
        monkeypatch,
        slug="docs-1",
        silos=[{"slug": "docs-1", "path": str(silo_root)}],
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    _patch_ingest(monkeypatch, update=_boom)

    res = mcp_module.update_file("docs", str(target), confirm=True)
    assert res["status"] == "error"
    assert "RuntimeError: disk full" in res["error"]
    from state import get_last_failures

    failures = get_last_failures(mcp_module._DB_PATH)
    assert len(failures) == 1
    assert failures[0]["path"] == str(target.resolve())
    assert "disk full" in failures[0]["error"]


# ---------- remove_file ----------


def test_remove_file_requires_confirm(mcp_module):
    res = mcp_module.remove_file("docs", "/x", confirm=False)
    assert res["status"] == "not_started"


def test_remove_file_path_outside_silo_root(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    _patch_state(
        monkeypatch,
        slug="docs-1",
        silos=[{"slug": "docs-1", "path": str(silo_root)}],
    )
    res = mcp_module.remove_file("docs", str(tmp_path / "outside.txt"), confirm=True)
    assert res["status"] == "error"
    assert "not under silo source" in res["error"]


def test_remove_file_happy_path_calls_remove_single_file(monkeypatch, mcp_module, tmp_path):
    silo_root = tmp_path / "silo"
    silo_root.mkdir()
    target = silo_root / "stale.md"
    # File doesn't need to exist on disk for remove — manifest cleanup operates on path string.
    _patch_state(
        monkeypatch,
        slug="docs-1",
        silos=[{"slug": "docs-1", "path": str(silo_root)}],
    )
    calls: list[tuple] = []

    def _fake_remove(path, db_path, silo_slug):
        calls.append((str(path), str(db_path), silo_slug))
        return ("removed", str(path))

    _patch_ingest(monkeypatch, remove=_fake_remove)

    res = mcp_module.remove_file("docs", str(target), confirm=True)

    assert res["status"] == "removed"
    assert res["silo"] == "docs-1"
    assert len(calls) == 1
    assert calls[0][2] == "docs-1"
