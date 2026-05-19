from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_SERVER_STARTED_AT", "2026-05-19T12:00:00+00:00")
    monkeypatch.setattr(mcp_server, "_package_version", lambda: "0.1.0-test")
    return mcp_server


def test_healthz_returns_probe_fields(mcp_module):
    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
    request = Request(scope)

    async def _run():
        response = await mcp_module.healthz(request)
        body = json.loads(response.body)
        assert response.status_code == 200
        assert body == {
            "ok": True,
            "service": "llmLibrarian-mcp",
            "version": "0.1.0-test",
            "db_exists": True,
            "started_at": "2026-05-19T12:00:00+00:00",
        }

    asyncio.run(_run())


def test_healthz_db_missing(mcp_module, monkeypatch, tmp_path):
    missing = tmp_path / "missing_db"
    monkeypatch.setattr(mcp_module, "_DB_PATH", str(missing))

    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": []}
    request = Request(scope)

    async def _run():
        response = await mcp_module.healthz(request)
        body = json.loads(response.body)
        assert body["db_exists"] is False

    asyncio.run(_run())
