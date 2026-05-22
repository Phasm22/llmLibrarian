"""Coverage for mcp_runtime_status runtime visibility payload."""

from __future__ import annotations

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    return mcp_server


def test_mcp_runtime_status_shape(monkeypatch, mcp_module):
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "chroma_server_host": "127.0.0.1",
            "chroma_server_port": 8000,
            "query_health": {"recent_error_count": 2},
            "ingest_failures": {"last_failure_count": 1},
            "hnsw_consistency": {"desynced_count": 0},
            "active_background_jobs": {"docs": {"kind": "trigger_reindex"}},
            "last_background_reindex": {"docs": {"ok": True}},
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {
            "pid_lock_path": "/tmp/llmlibrarian-mcp-1000.pid",
            "lock_file_exists": True,
            "lock_holder_pid": 123,
            "lock_holder_alive": True,
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {"mcp_process_count": 1, "multiple_mcp_processes": False},
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: ["from-health"])

    out = mcp_module.mcp_runtime_status()

    assert out["db_exists"] is True
    assert out["mcp_http"]["lock_holder_pid"] == 123
    assert out["chroma"]["transport"] == "http"
    assert out["health_counts"] == {
        "query_error_count": 2,
        "ingest_failure_count": 1,
        "hnsw_desynced_count": 0,
    }
    assert out["jobs"]["active_count"] == 1
    assert out["jobs"]["active_background_jobs"]["docs"]["kind"] == "trigger_reindex"
    assert "from-health" in out["recommended_actions"]


def test_mcp_runtime_status_adds_runtime_actions(monkeypatch, mcp_module):
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "query_health": {"recent_error_count": 0},
            "ingest_failures": {"last_failure_count": 0},
            "hnsw_consistency": {"desynced_count": 0},
            "active_background_jobs": {},
            "last_background_reindex": {},
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {
            "pid_lock_path": "/tmp/llmlibrarian-mcp-1000.pid",
            "lock_file_exists": True,
            "lock_holder_pid": 4321,
            "lock_holder_alive": False,
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {"mcp_process_count": 3, "multiple_mcp_processes": True},
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: [])

    out = mcp_module.mcp_runtime_status()
    actions = out["recommended_actions"]

    assert any("Multiple mcp_server.py processes" in row for row in actions)
    assert any("PID lock file points to a dead process" in row for row in actions)


def test_mcp_runtime_status_verbose_includes_summary(monkeypatch, mcp_module):
    summary = {
        "db_exists": True,
        "chroma_transport": "embedded",
        "chroma_server_ok": True,
        "query_health": {"recent_error_count": 0},
        "ingest_failures": {"last_failure_count": 0},
        "hnsw_consistency": {"desynced_count": 0},
        "active_background_jobs": {},
        "last_background_reindex": {},
    }
    monkeypatch.setattr(mcp_module, "_collect_health_summary", lambda include_audit=False: summary)
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {"pid_lock_path": "/tmp/x", "lock_file_exists": False, "lock_holder_pid": None, "lock_holder_alive": None},
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {
            "mcp_process_count": 1,
            "multiple_mcp_processes": False,
            **({"processes": [{"pid": 1, "cmdline": "python mcp_server.py"}]} if verbose else {}),
        },
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: [])

    out = mcp_module.mcp_runtime_status(verbose=True)

    assert out["summary_raw"] == summary
    assert "processes" in out["mcp_http"]
