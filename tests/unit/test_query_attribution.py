"""Who asked: caller attribution for the query audit (src/query_attribution.py).

The properties that matter: the label prefers what the caller declares over
what we infer, the stdio identity carries the Claude Code session id but none
of the secrets that share its env, the HTTP path reads tags and headers, and a
lookup failure never breaks the tool call it is attributing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import query_attribution as qa


@pytest.fixture(autouse=True)
def _fresh_caches():
    qa._peer_cache.clear()
    qa._handshakes.clear()
    yield
    qa._peer_cache.clear()
    qa._handshakes.clear()


@pytest.mark.parametrize(
    "client,label",
    [
        ({"tag": "kitchen-agent", "client_name": "claude-code", "app": "claude"}, "kitchen-agent"),
        ({"client_name": "claude-code", "app": "claude"}, "Claude Code"),
        ({"client_name": "claude-ai", "app": "Claude"}, "Claude Desktop"),
        ({"client_name": "anythingllm-mcp", "app": "AnythingLLM"}, "AnythingLLM"),
        ({"client_name": "some-new-client", "app": "Zed"}, "Zed"),
        ({"client_name": "some-new-client"}, "some-new-client"),
        ({"app": "claude"}, "Claude Code"),
        ({"user_agent": "node-fetch/1.0 (+https://x)"}, "node-fetch"),
        ({"transport": "http"}, "http"),
        ({}, "unknown"),
    ],
)
def test_label_precedence(client, label):
    assert qa.client_label(client) == label


def test_app_from_command_uses_outermost_bundle():
    helper = (
        "/Applications/AnythingLLM.app/Contents/Frameworks/"
        "AnythingLLM Helper.app/Contents/MacOS/AnythingLLM Helper"
    )
    assert qa._app_from_command(helper) == "AnythingLLM"
    assert qa._app_from_command("/usr/local/bin/cursor") == "cursor"


def test_app_for_pid_skips_wrappers(monkeypatch):
    rows = {
        50: (40, "/Users/x/.venv/bin/python"),
        40: (30, "uv"),
        30: (1, "/Users/x/Library/Application Support/Claude/claude-code/2.1/claude.app/Contents/MacOS/claude"),
    }
    monkeypatch.setattr(qa, "_ps_parent_and_command", lambda pid: rows.get(pid))
    assert qa.app_for_pid(50) == "claude"
    assert qa.app_for_pid(99) is None


def test_stdio_identity_whitelists_env(monkeypatch):
    monkeypatch.setattr(qa, "client_app_name", lambda: "claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc6e0e3e-session")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    monkeypatch.setenv("CLAUDE_CODE_USER_EMAIL", "someone@example.com")

    identity = qa.static_stdio_identity()

    assert identity["session"] == "cc6e0e3e-session"
    assert identity["entrypoint"] == "cli"
    assert identity["app"] == "claude"
    blob = json.dumps(identity)
    assert "secret" not in blob and "example.com" not in blob


def test_parse_lsof_pids_drops_self_and_dupes():
    out = "p1334\ncPython\np628\ncAnythingLLM Helper\np628\n"
    assert qa.parse_lsof_pids(out, exclude_pid=1334) == [628]


def test_peer_for_port_caches_and_survives_lsof_failure(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout="p1\np777\n")

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    monkeypatch.setattr(qa.os, "getpid", lambda: 1)
    monkeypatch.setattr(qa, "app_for_pid", lambda pid: "AnythingLLM")

    assert qa.peer_for_port(50123) == {"pid": 777, "app": "AnythingLLM"}
    assert qa.peer_for_port(50123) == {"pid": 777, "app": "AnythingLLM"}
    assert len(calls) == 1

    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no lsof")))
    assert qa.peer_for_port(50124) is None


def _probe_server():
    from fastmcp import FastMCP

    import mcp_server

    server = FastMCP("attribution-test")
    server.add_middleware(mcp_server._CallerAttribution())

    @server.tool()
    def whoami() -> dict:
        return {"caller": mcp_server._CALLER.get()}

    return server


def test_stdio_call_carries_client_info_and_static_identity(monkeypatch):
    from fastmcp import Client
    from mcp.types import Implementation

    import mcp_server

    monkeypatch.setattr(mcp_server, "_STDIO_IDENTITY", {"transport": "stdio", "app": "claude", "session": "s-1"})

    async def call() -> dict:
        async with Client(_probe_server(), client_info=Implementation(name="claude-code", version="2.1.295")) as c:
            return (await c.call_tool("whoami", {})).data

    caller = asyncio.run(call())["caller"]
    assert caller["client_name"] == "claude-code"
    assert caller["client_version"] == "2.1.295"
    assert caller["session"] == "s-1"
    assert caller["label"] == "Claude Code"


def test_http_call_reads_tag_and_user_agent(monkeypatch):
    from starlette.testclient import TestClient

    app = _probe_server().http_app(path="/mcp", stateless_http=True)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "user-agent": "node/22",
        "mcp-protocol-version": "2025-06-18",
    }
    with TestClient(app) as client:
        resp = client.post("/mcp?client=kitchen-agent", json=body, headers=headers)
    payload = next(
        json.loads(line[len("data:"):]) for line in resp.text.splitlines() if line.startswith("data:")
    )
    caller = payload["result"]["structuredContent"]["caller"]
    assert caller["transport"] == "http"
    assert caller["tag"] == "kitchen-agent"
    assert caller["user_agent"] == "node/22"
    assert caller["label"] == "kitchen-agent"


def test_attribution_failure_does_not_break_the_call(monkeypatch):
    from fastmcp import Client

    import mcp_server

    monkeypatch.setattr(mcp_server, "_stdio_identity", lambda: (_ for _ in ()).throw(RuntimeError("ps gone")))

    async def call() -> dict:
        async with Client(_probe_server()) as c:
            return (await c.call_tool("whoami", {})).data

    assert asyncio.run(call())["caller"] is None
