"""MCP client env fallback: pal must reach a server mounted on a non-default URL path.

Regression tests for the "initial MCP add_silo failed: Session terminated" bug:
the server's .env.mcp configured a secret LLMLIBRARIAN_MCP_PATH, pal clients in a
fresh shell defaulted to /mcp, and the resulting 404 surfaced as an opaque
transport-level error.
"""
import os
from pathlib import Path

import pytest

import pal

_MCP_KEYS = [
    "LLMLIBRARIAN_MCP_URL",
    "LLMLIBRARIAN_MCP_HOST",
    "LLMLIBRARIAN_MCP_PORT",
    "LLMLIBRARIAN_MCP_PATH",
    "LLMLIBRARIAN_MCP_BEARER_TOKEN",
]


@pytest.fixture(autouse=True)
def _clean_mcp_env():
    # _ensure_mcp_client_env mutates os.environ directly, which
    # monkeypatch.delenv(raising=False) does not track for absent keys —
    # snapshot and restore explicitly so nothing leaks across tests.
    keys = _MCP_KEYS + ["LLMLI_MCP_ENV_FILE"]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _write_env_mcp(tmp_path: Path, extra: str = "") -> Path:
    env_file = tmp_path / ".env.mcp"
    env_file.write_text(
        "LLMLIBRARIAN_MCP_TRANSPORT=streamable-http\n"
        "LLMLIBRARIAN_MCP_HOST=0.0.0.0\n"
        "LLMLIBRARIAN_MCP_PORT=8765\n"
        "LLMLIBRARIAN_MCP_PATH=/secret123/mcp\n"
        + extra,
        encoding="utf-8",
    )
    return env_file


def test_mcp_url_loads_secret_path_from_env_mcp(monkeypatch, tmp_path):
    env_file = _write_env_mcp(tmp_path)
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])

    url = pal._mcp_url()
    assert url == "http://127.0.0.1:8765/secret123/mcp"


def test_mcp_url_maps_wildcard_bind_host_to_loopback(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_PATH", "/mcp")
    assert pal._mcp_url() == "http://127.0.0.1:8765/mcp"


def test_shell_env_wins_over_env_mcp(monkeypatch, tmp_path):
    env_file = _write_env_mcp(tmp_path)
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])
    monkeypatch.setenv("LLMLIBRARIAN_MCP_PATH", "/override/mcp")

    assert pal._mcp_url() == "http://127.0.0.1:8765/override/mcp"


def test_explicit_url_wins_over_everything(monkeypatch, tmp_path):
    env_file = _write_env_mcp(tmp_path)
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])
    monkeypatch.setenv("LLMLIBRARIAN_MCP_URL", "http://10.0.0.9:9999/x/mcp")

    assert pal._mcp_url() == "http://10.0.0.9:9999/x/mcp"


def test_bearer_token_derived_from_auth_token_when_auth_required(monkeypatch, tmp_path):
    env_file = _write_env_mcp(
        tmp_path,
        extra="LLMLIBRARIAN_MCP_REQUIRE_AUTH=true\nLLMLIBRARIAN_MCP_AUTH_TOKEN=tok-abc\n",
    )
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])

    assert pal._mcp_bearer_token() == "tok-abc"


def test_no_bearer_token_when_auth_not_required(monkeypatch, tmp_path):
    env_file = _write_env_mcp(
        tmp_path,
        extra="LLMLIBRARIAN_MCP_REQUIRE_AUTH=false\nLLMLIBRARIAN_MCP_AUTH_TOKEN=tok-abc\n",
    )
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])

    assert pal._mcp_bearer_token() is None


def test_missing_env_mcp_falls_back_to_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [tmp_path / "nope" / ".env.mcp"])
    assert pal._mcp_url() == "http://127.0.0.1:8765/mcp"


def test_mcp_call_sync_diagnoses_404_path_mismatch(monkeypatch):
    async def _boom(tool, **args):
        raise RuntimeError("Session terminated")

    monkeypatch.setattr(pal, "_mcp_call", _boom)
    monkeypatch.setattr(pal, "_mcp_endpoint_http_status", lambda: 404)
    monkeypatch.setattr(pal, "_mcp_url", lambda: "http://127.0.0.1:8765/mcp")

    with pytest.raises(RuntimeError) as excinfo:
        pal._mcp_call_sync("add_silo", path="/tmp/x", confirm=True)
    msg = str(excinfo.value)
    assert "404" in msg
    assert "LLMLIBRARIAN_MCP_PATH" in msg


def test_mcp_call_sync_reraises_other_errors_unchanged(monkeypatch):
    async def _boom(tool, **args):
        raise RuntimeError("Session terminated")

    monkeypatch.setattr(pal, "_mcp_call", _boom)
    monkeypatch.setattr(pal, "_mcp_endpoint_http_status", lambda: 405)

    with pytest.raises(RuntimeError, match="Session terminated"):
        pal._mcp_call_sync("health")


def test_healthcheck_fails_fast_on_path_mismatch(monkeypatch):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: _Resp())
    monkeypatch.setattr(pal, "_mcp_endpoint_http_status", lambda: 404)
    monkeypatch.setattr(pal, "_mcp_url", lambda: "http://127.0.0.1:8765/mcp")
    monkeypatch.setattr(pal, "_mcp_bearer_token", lambda: None)

    ok, msg = pal._mcp_healthcheck()
    assert ok is False
    assert "LLMLIBRARIAN_MCP_PATH" in msg


def test_daemon_env_bakes_mcp_keys_from_candidate_env_file(monkeypatch, tmp_path):
    env_file = _write_env_mcp(
        tmp_path,
        extra="LLMLIBRARIAN_CHROMA_HOST=127.0.0.1\nLLMLIBRARIAN_CHROMA_PORT=8000\n",
    )
    monkeypatch.setattr(pal, "_mcp_env_file_candidates", lambda: [env_file])
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_PORT", raising=False)

    env = pal._daemon_env(tmp_path / "db")
    assert env["LLMLIBRARIAN_MCP_PATH"] == "/secret123/mcp"
    assert env["LLMLIBRARIAN_MCP_PORT"] == "8765"
    assert env["LLMLIBRARIAN_CHROMA_HOST"] == "127.0.0.1"
