"""MCP client env fallback: pal must reach a server mounted on a non-default URL path.

Regression tests for the "initial MCP add_silo failed: Session terminated" bug:
the server's .env.mcp configured a secret LLMLIBRARIAN_MCP_PATH, pal clients in a
fresh shell defaulted to /mcp, and the resulting 404 surfaced as an opaque
transport-level error.
"""
import json
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
    def _boom(tool, **args):
        raise pal._MCPHTTPError(404, "Not Found")

    monkeypatch.setattr(pal, "_mcp_call", _boom)
    monkeypatch.setattr(pal, "_mcp_url", lambda: "http://127.0.0.1:8765/mcp")

    with pytest.raises(RuntimeError) as excinfo:
        pal._mcp_call_sync("add_silo", path="/tmp/x", confirm=True)
    msg = str(excinfo.value)
    assert "404" in msg
    assert "LLMLIBRARIAN_MCP_PATH" in msg


def test_mcp_call_sync_diagnoses_lite_profile_missing_write_tool(monkeypatch):
    """A lite-profile server passes /healthz but has no write tools.

    The raw server error is just "Unknown tool: 'add_silo'", which points at
    pal rather than at the server's profile; name the real cause instead.
    """
    def _boom(tool, **args):
        raise RuntimeError("Unknown tool: 'add_silo'")

    monkeypatch.setattr(pal, "_mcp_call", _boom)
    monkeypatch.setattr(pal, "_mcp_url", lambda: "http://127.0.0.1:8766/mcp")

    with pytest.raises(RuntimeError) as excinfo:
        pal._mcp_call_sync("add_silo", path="/tmp/x", confirm=True)
    msg = str(excinfo.value)
    assert "LLMLIBRARIAN_MCP_PROFILE=lite" in msg
    assert "add_silo" in msg


def test_mcp_call_sync_reraises_other_errors_unchanged(monkeypatch):
    def _boom(tool, **args):
        raise pal._MCPHTTPError(500, "boom")

    monkeypatch.setattr(pal, "_mcp_call", _boom)

    with pytest.raises(RuntimeError, match="MCP HTTP 500"):
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


def _serve_once(reply_body: bytes, content_type: str):
    import http.server
    import threading

    seen: dict = {}

    class _H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["accept"] = self.headers.get("Accept")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(reply_body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, seen


@pytest.mark.parametrize("framing", ["sse", "json"])
def test_mcp_call_parses_structured_content(monkeypatch, framing):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": '{"status":"unchanged"}'}],
            "structuredContent": {"status": "unchanged", "silo": "s"},
            "isError": False,
        },
    }
    if framing == "sse":
        srv, seen = _serve_once(f"event: message\ndata: {json.dumps(msg)}\n\n".encode(), "text/event-stream")
    else:
        srv, seen = _serve_once(json.dumps(msg).encode(), "application/json")
    monkeypatch.setattr(pal, "_mcp_url", lambda: f"http://127.0.0.1:{srv.server_port}/mcp")
    monkeypatch.setattr(pal, "_mcp_bearer_token", lambda: None)

    assert pal._mcp_call("update_file", silo="s", path="/x", confirm=True) == {"status": "unchanged", "silo": "s"}
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {"name": "update_file", "arguments": {"silo": "s", "path": "/x", "confirm": True}}
    assert "text/event-stream" in seen["accept"]
    srv.server_close()


def test_mcp_call_raises_tool_error_text(monkeypatch):
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Unknown tool: 'add_silo'"}], "isError": True}}
    srv, _seen = _serve_once(f"data: {json.dumps(msg)}\n\n".encode(), "text/event-stream")
    monkeypatch.setattr(pal, "_mcp_url", lambda: f"http://127.0.0.1:{srv.server_port}/mcp")
    monkeypatch.setattr(pal, "_mcp_bearer_token", lambda: None)

    with pytest.raises(RuntimeError, match="LLMLIBRARIAN_MCP_PROFILE=lite"):
        pal._mcp_call_sync("add_silo", path="/tmp/x", confirm=True)
    srv.server_close()
