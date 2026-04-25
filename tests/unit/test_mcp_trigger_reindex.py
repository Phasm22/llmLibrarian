"""Coverage for the trigger_reindex MCP tool's guard rails.

Covers the synchronous return-value paths only — the background thread
that calls ingest.run_add is integration-territory and not asserted here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    """Import mcp_server with a clean DB path pointed at tmp_path."""
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    return mcp_server


def _patch_state(monkeypatch, *, slug, silos):
    fake_state = SimpleNamespace(
        list_silos=lambda _db: silos,
        resolve_silo_to_slug=lambda _db, _name: slug,
    )
    monkeypatch.setitem(sys.modules, "state", fake_state)


def test_trigger_reindex_db_missing(monkeypatch, tmp_path):
    import mcp_server

    missing = tmp_path / "missing"
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(missing))

    res = mcp_server.trigger_reindex("anything", confirm=True)

    assert res["status"] == "error"
    assert "does not exist" in res["error"]


def test_trigger_reindex_requires_confirm(mcp_module):
    res = mcp_module.trigger_reindex("docs", confirm=False)

    assert res["status"] == "not_started"
    assert "confirm=True" in res["message"]


def test_trigger_reindex_silo_not_found(monkeypatch, mcp_module):
    _patch_state(monkeypatch, slug=None, silos=[])

    res = mcp_module.trigger_reindex("ghost", confirm=True)

    assert res["status"] == "error"
    assert "silo not found" in res["error"]
    assert "'ghost'" in res["error"]


def test_trigger_reindex_registry_entry_missing(monkeypatch, mcp_module):
    _patch_state(monkeypatch, slug="docs-1234", silos=[{"slug": "other-5678"}])

    res = mcp_module.trigger_reindex("docs", confirm=True)

    assert res["status"] == "error"
    assert "registry entry missing" in res["error"]
    assert "docs-1234" in res["error"]


def test_trigger_reindex_no_registered_path(monkeypatch, mcp_module):
    _patch_state(
        monkeypatch,
        slug="docs-1234",
        silos=[{"slug": "docs-1234", "path": ""}],
    )

    res = mcp_module.trigger_reindex("docs", confirm=True)

    assert res["status"] == "error"
    assert "no registered source path" in res["error"]


def test_trigger_reindex_source_path_does_not_exist(monkeypatch, mcp_module, tmp_path):
    missing = tmp_path / "vanished"
    _patch_state(
        monkeypatch,
        slug="docs-1234",
        silos=[{"slug": "docs-1234", "path": str(missing)}],
    )

    res = mcp_module.trigger_reindex("docs", confirm=True)

    assert res["status"] == "error"
    assert "source path does not exist" in res["error"]
    assert str(missing) in res["error"]


def test_trigger_reindex_happy_path_returns_started(monkeypatch, mcp_module, tmp_path):
    """Happy path: return value indicates background job started.

    The background thread itself is intercepted so we don't actually open
    Chroma — we just verify the synchronous return contract.
    """
    src = tmp_path / "docs"
    src.mkdir()
    _patch_state(
        monkeypatch,
        slug="docs-1234",
        silos=[
            {
                "slug": "docs-1234",
                "path": str(src),
                "display_name": "Docs",
            }
        ],
    )

    started_threads = []

    class _NoopThread:
        def __init__(self, target=None, daemon=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            started_threads.append(self)

    monkeypatch.setattr(mcp_module.threading, "Thread", _NoopThread)

    res = mcp_module.trigger_reindex("docs", confirm=True)

    assert res["status"] == "started"
    assert res["silo"] == "docs-1234"
    assert res["display_name"] == "Docs"
    assert res["path"] == str(src)
    assert len(started_threads) == 1
    assert started_threads[0].daemon is True
