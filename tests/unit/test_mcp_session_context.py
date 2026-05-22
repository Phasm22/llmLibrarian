"""Coverage for session_context bootstrap and recommendation rules."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    return mcp_server


def test_derive_recommended_actions_db_missing(mcp_module):
    out = mcp_module._derive_recommended_actions([], {"db_exists": False})
    assert out
    assert "Fix LLMLIBRARIAN_DB" in out[0]


def test_derive_recommended_actions_stale_vs_race_noise(mcp_module):
    silos = [
        {
            "slug": "small-race",
            "is_stale": True,
            "stale_file_count": 2,
            "newest_source_mtime_iso": "2026-05-22T20:00:00+00:00",
            "updated": "2026-05-22T20:00:00+00:00",
        },
        {
            "slug": "really-stale",
            "is_stale": True,
            "stale_file_count": 9,
            "newest_source_mtime_iso": "2026-05-22T21:00:00+00:00",
            "updated": "2026-05-20T21:00:00+00:00",
            "has_index_errors": True,
        },
    ]
    summary = {
        "db_exists": True,
        "chroma_transport": "http",
        "chroma_server_ok": True,
        "query_health": {"recent_error_count": 0},
        "ingest_failures": {"last_failure_count": 0},
        "hnsw_consistency": {"desynced_count": 0},
    }

    out = mcp_module._derive_recommended_actions(silos, summary)

    assert any("small-race" in row and "noise" in row for row in out)
    assert any("really-stale" in row and "trigger_reindex" in row for row in out)
    assert any("really-stale" in row and "repair_silo" in row for row in out)


def test_session_context_merges_list_and_summary(monkeypatch, mcp_module):
    calls = {"staleness": None}

    def _fake_list(_db_path, check_staleness=False):
        calls["staleness"] = check_staleness
        return {
            "db_path": "/tmp/db",
            "db_exists": True,
            "silo_count": 1,
            "silos": [{"slug": "docs", "chunks_count": 10, "is_stale": False}],
        }

    monkeypatch.setitem(
        sys.modules,
        "operations",
        SimpleNamespace(op_list_silos=_fake_list),
    )
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "hnsw_consistency": {"desynced_count": 0},
        },
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: ["ok"])

    out = mcp_module.session_context(check_staleness=True, include_audit=False)

    assert calls["staleness"] is True
    assert out["silo_count"] == 1
    assert out["health_summary"]["chroma_transport"] == "http"
    assert out["recommended_actions"] == ["ok"]
    assert out["ready_for_retrieval"] is True
