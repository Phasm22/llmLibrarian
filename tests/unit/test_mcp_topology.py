"""The deployment model must be observable and the shipped configs consistent.

Docs, .mcp.json, manifest.json, and the running process previously disagreed
about which transport and which Chroma mode were in play. /healthz is the one
place another process can ask the live server instead of guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mcp_server

ROOT = Path(__file__).resolve().parents[2]


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


# ---------------------------------------------------------------------------
# Shipped configs
# ---------------------------------------------------------------------------


def test_plugin_mcp_config_matches_root():
    """plugin/.mcp.json is a copy of .mcp.json. Keeping them byte-identical is
    what stops the two from drifting into different servers."""
    root = (ROOT / ".mcp.json").read_text()
    plugin = (ROOT / "plugin" / ".mcp.json").read_text()
    assert root == plugin, "plugin/.mcp.json has drifted from .mcp.json"


def _launch_values(cfg: dict) -> list[str]:
    """Command/args/env values a host would actually execute or export.

    Descriptions and help text are excluded — manifest.json legitimately shows
    an example path to the user.
    """
    servers = cfg.get("mcpServers") or {"_": (cfg.get("server") or {}).get("mcp_config", {})}
    out: list[str] = []
    for spec in servers.values():
        out.append(str(spec.get("command", "")))
        out.extend(str(a) for a in spec.get("args", []))
        out.extend(str(v) for v in (spec.get("env") or {}).values())
    return out


def test_shipped_configs_carry_no_absolute_home_paths():
    for rel in (".mcp.json", "plugin/.mcp.json", "manifest.json"):
        cfg = json.loads((ROOT / rel).read_text())
        for value in _launch_values(cfg):
            assert "/home/" not in value, f"{rel} hardcodes a machine-specific path: {value}"
            assert "/Users/" not in value, f"{rel} hardcodes a machine-specific path: {value}"


def test_env_example_carries_no_foreign_home_path():
    text = (ROOT / ".env.mcp.example").read_text()
    assert "/Users/tjm4" not in text


def test_env_example_enables_chroma_server_mode():
    """Server mode is required whenever the HTTP service runs; shipping it
    commented out as 'optional' contradicted every doc."""
    text = (ROOT / ".env.mcp.example").read_text()
    assert "\nLLMLIBRARIAN_CHROMA_HOST=" in text


def test_version_is_consistent_across_manifests():
    manifest = json.loads((ROOT / "manifest.json").read_text())["version"]
    plugin = json.loads((ROOT / "plugin" / "plugin.json").read_text())["version"]
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert manifest == plugin
    assert f'version = "{manifest}"' in pyproject
