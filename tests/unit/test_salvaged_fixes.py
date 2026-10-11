"""Fixes salvaged from PR #7 (mcp runtime split) onto today's dev.

The split itself no longer applies — mcp_server.py has moved on by ~1,000 lines
since 2026-08-07 — but these three were standalone and still true on dev.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("payload", ["{pid}", None])
def test_watch_guard_sees_legacy_and_symlinked_locks(monkeypatch, tmp_path, payload):
    """A bare-integer lock (pal's legacy form) or a lock recording the DB through
    a symlink must still count as a live watcher, or the embedded-write guard
    lets a write through while the watcher holds the index."""
    import chroma_client

    real_db = tmp_path / "real" / "my_brain_db"
    real_db.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    locks = tmp_path / "pal" / "watch_locks"
    locks.mkdir(parents=True)
    pid = os.getpid()
    body = str(pid) if payload else json.dumps({"pid": pid, "silo": "s", "db_path": str(link / "my_brain_db")})
    (locks / "s-abc.pid").write_text(body)
    monkeypatch.setenv("PAL_HOME", str(tmp_path / "pal"))

    active = chroma_client._active_watch_processes_for_db(str(real_db))
    assert active and f"pid={pid}" in active[0]


def test_watch_guard_ignores_a_lock_for_another_db(monkeypatch, tmp_path):
    import chroma_client

    locks = tmp_path / "pal" / "watch_locks"
    locks.mkdir(parents=True)
    (locks / "s.pid").write_text(json.dumps({"pid": os.getpid(), "db_path": str(tmp_path / "other")}))
    monkeypatch.setenv("PAL_HOME", str(tmp_path / "pal"))
    assert chroma_client._active_watch_processes_for_db(str(tmp_path / "mine")) == []


@pytest.mark.parametrize(
    "host,auth,shows_path",
    [("127.0.0.1", "false", True), ("0.0.0.0", "false", False), ("0.0.0.0", "true", True)],
)
def test_healthz_withholds_db_path_when_exposed_without_auth(monkeypatch, host, auth, shows_path):
    from starlette.testclient import TestClient

    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", host)
    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", auth)
    with TestClient(mcp_server.mcp.http_app(path="/mcp")) as client:
        body = client.get("/healthz").json()
    assert body["ok"] and ("db_path" in body) is shows_path
    assert body.get("db_path_withheld", False) is (not shows_path)


def test_embedded_write_guard_fails_closed_on_a_withheld_db_path(monkeypatch, tmp_path):
    """Withholding the path must not read as "older server, stay permissive":
    an embedded write beside a live server on the same DB is the SIGSEGV case."""
    import chroma_client

    monkeypatch.setattr(chroma_client, "_mcp_healthz_info", lambda timeout=1.0: (True, chroma_client._DB_PATH_WITHHELD, False))
    assert "does not publish its DB path" in chroma_client._mcp_blocks_embedded_write(str(tmp_path))


def test_add_silo_requires_confirm(monkeypatch, tmp_path):
    """It was the one write tool exempt from its own guard, and the one that
    indexes an arbitrary path."""
    import mcp_server

    started = []
    monkeypatch.setattr("threading.Thread.start", lambda self: started.append(1))
    out = mcp_server.add_silo(str(tmp_path))
    assert out["status"] == "not_started" and not started
