"""The deployment model must be observable from the running server.

Docs, config on disk, and the live process previously disagreed about which
transport and which Chroma mode were in play. /healthz is the one place another
process can ask instead of guessing — the embedded-write guard depends on it.
"""

from __future__ import annotations

import pytest

import mcp_server


@pytest.fixture
def db(monkeypatch, tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(d))
    return d


def test_healthz_reports_transport_and_chroma_mode(db, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "127.0.0.1")

    payload = mcp_server._healthz_payload()
    assert payload["transport"] == "streamable-http"
    assert payload["chroma_transport"] == "http"


def test_healthz_reports_embedded_when_no_chroma_host(db, monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    assert mcp_server._healthz_payload()["chroma_transport"] == "embedded"


def test_healthz_exposes_db_path_on_loopback(db, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "127.0.0.1")
    monkeypatch.delenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", raising=False)
    assert mcp_server._healthz_payload()["db_path"] == str(db)


def test_healthz_withholds_db_path_when_exposed_without_auth(db, monkeypatch):
    """/healthz has no auth check of its own and may be published via Tailscale
    Funnel, so an absolute filesystem path must not leak off-host."""
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "false")

    payload = mcp_server._healthz_payload()
    assert "db_path" not in payload
    assert "db_path_withheld" in payload
    assert payload["ok"] is True


def test_healthz_exposes_db_path_when_auth_required(db, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    assert mcp_server._healthz_payload()["db_path"] == str(db)


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("10.0.0.5", False)],
)
def test_loopback_detection(monkeypatch, host, expected):
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", host)
    assert runtime.is_loopback_bind() is expected
