"""scripts/smoke_deploy.py: each check, the exit status, and token hygiene.

A fake MCP server runs behind httpx.MockTransport, so no socket is opened.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_deploy.py"
_spec = importlib.util.spec_from_file_location("smoke_deploy", SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)

URL = "https://llmli.example.test/mcp"
TOKEN = "SECRET-TOKEN-do-not-print-7f3a"


def _fake_server(
    *,
    healthz_ok=True,
    auth_enforced=True,
    tools=("query_personal_knowledge",),
    chunks=2,
    sse=False,
    search_error=False,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": healthz_ok, "version": "9.9", "image_backend": "ok"})
        assert request.url.path == "/mcp"
        if auth_enforced and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": smoke.PROTOCOL_VERSION}},
                headers={"mcp-session-id": "sess-1"},
            )
        if method == "tools/list":
            result = {"tools": [{"name": name} for name in tools]}
        elif method == "tools/call":
            payload = {"chunks": [{"text": "x"}] * chunks}
            if search_error:
                payload = {"error": "db_missing", "chunks": []}
            result = {"structuredContent": payload, "isError": False}
        else:
            return httpx.Response(400, json={"error": "bad"})
        envelope = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            text = f"event: message\ndata: {json.dumps(envelope)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=envelope)

    return handler


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(handler, query="overview"):
    with _client(handler) as client:
        return {r.name: r for r in smoke.run_checks(client, URL, TOKEN, query)}


def test_all_checks_pass_against_a_healthy_server():
    results = _run(_fake_server())
    assert all(r.ok for r in results.values()), results
    assert len(results) == 4


def test_sse_framed_responses_are_parsed():
    results = _run(_fake_server(sse=True))
    assert all(r.ok for r in results.values()), results


def test_healthz_ok_false_fails():
    results = _run(_fake_server(healthz_ok=False))
    assert not results["healthz ok"].ok


def test_unauthenticated_call_that_succeeds_fails():
    results = _run(_fake_server(auth_enforced=False))
    failed = results["unauthenticated MCP call -> 401"]
    assert not failed.ok and "200" in failed.detail


def test_empty_tool_list_fails():
    results = _run(_fake_server(tools=()))
    assert not results["authenticated tools/list"].ok


def test_search_with_zero_chunks_fails():
    results = _run(_fake_server(chunks=0))
    assert not results["search returns results"].ok


def test_search_error_field_fails():
    results = _run(_fake_server(search_error=True))
    assert not results["search returns results"].ok


def test_healthz_url_is_the_origin_path():
    assert smoke.healthz_url("https://h.example:8443/lite/mcp") == "https://h.example:8443/healthz"


def test_main_exits_zero_only_when_every_check_passes(monkeypatch, capsys):
    monkeypatch.setenv("LLMLI_SMOKE_TOKEN", TOKEN)
    ok = smoke.main([URL], make_client=lambda t: _client(_fake_server()))
    assert ok == 0
    bad = smoke.main([URL], make_client=lambda t: _client(_fake_server(chunks=0)))
    assert bad == 1


def test_main_requires_the_token_env_and_never_prints_it(monkeypatch, capsys):
    monkeypatch.delenv("LLMLI_SMOKE_TOKEN", raising=False)
    assert smoke.main([URL]) == 2

    monkeypatch.setenv("LLMLI_SMOKE_TOKEN", TOKEN)
    assert smoke.main([URL], make_client=lambda t: _client(_fake_server(chunks=0))) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_token_is_not_printed_even_when_the_server_is_down(monkeypatch, capsys):
    monkeypatch.setenv("LLMLI_SMOKE_TOKEN", TOKEN)

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert smoke.main([URL], make_client=lambda t: _client(refuse)) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert captured.out.count("FAIL") == 4


def test_token_env_name_is_configurable(monkeypatch):
    monkeypatch.delenv("LLMLI_SMOKE_TOKEN", raising=False)
    monkeypatch.setenv("MY_TOKEN", TOKEN)
    assert smoke.main([URL, "--token-env", "MY_TOKEN"], make_client=lambda t: _client(_fake_server())) == 0
