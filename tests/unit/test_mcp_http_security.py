"""HTTP MCP security: fail-closed auth, read-default tools, write scopes, limits."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_security_env(monkeypatch):
    for key in (
        "LLMLIBRARIAN_MCP_REQUIRE_AUTH",
        "LLMLIBRARIAN_MCP_AUTH_TOKEN",
        "LLMLIBRARIAN_MCP_AUTH_TOKEN_SCOPES",
        "LLMLIBRARIAN_MCP_AUTH_TOKENS",
        "LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS",
        "LLMLIBRARIAN_MCP_BEARER_TOKEN",
        "LLMLIBRARIAN_MCP_HOST",
    ):
        monkeypatch.delenv(key, raising=False)


def test_require_auth_defaults_true():
    import mcp_http_security as sec

    assert sec.require_auth_enabled() is True


def test_auth_fail_closed_without_token(monkeypatch):
    import mcp_http_security as sec
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    with pytest.raises(RuntimeError, match="no token is configured"):
        sec.auth_required_and_configured()
    with pytest.raises(RuntimeError, match="no token is configured"):
        mcp_server._auth_for_transport("streamable-http")


def test_auth_disabled_returns_none_provider(monkeypatch):
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "false")
    assert mcp_server._auth_for_transport("streamable-http") is None
    assert mcp_server._auth_for_transport("stdio") is None


def test_single_token_defaults_to_read_scope(monkeypatch):
    import mcp_http_security as sec
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "tok-read")
    provider = mcp_server._auth_for_transport("streamable-http")
    assert provider is not None
    access = asyncio.run(provider.verify_token("tok-read"))
    assert access is not None
    assert access.client_id == "default"
    assert access.scopes == ["read"]
    assert asyncio.run(provider.verify_token("wrong")) is None


def test_auth_tokens_json_scopes(monkeypatch):
    import mcp_http_security as sec
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv(
        "LLMLIBRARIAN_MCP_AUTH_TOKENS",
        json.dumps(
            {
                "hermes": {"token": "t-hermes", "scopes": ["read"]},
                "pal": {"token": "t-pal", "scopes": ["read", "write"]},
            }
        ),
    )
    callers = sec.load_caller_tokens()
    assert {c.client_id for c in callers} == {"hermes", "pal"}
    provider = mcp_server._auth_for_transport("http")
    write = asyncio.run(provider.verify_token("t-pal"))
    assert write is not None
    assert "write" in write.scopes
    assert "read" in write.scopes


def test_write_tools_hidden_by_default_in_catalog():
    script = r"""
import asyncio, json, mcp_server
async def main():
    tools = await mcp_server.mcp.list_tools()
    print(json.dumps(sorted(t.name for t in tools)))
asyncio.run(main())
"""
    env = os.environ.copy()
    env["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
    env.pop("LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS", None)
    env.pop("LLMLIBRARIAN_MCP_PROFILE", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    names = json.loads(proc.stdout.strip().splitlines()[-1])
    for write_tool in (
        "add_silo",
        "trigger_reindex",
        "repair_silo",
        "update_file",
        "remove_file",
        "set_silo_privacy",
    ):
        assert write_tool not in names
    assert "query_personal_knowledge" in names
    assert "health" in names


def test_write_tools_present_when_enabled():
    script = r"""
import asyncio, json, mcp_server
async def main():
    tools = await mcp_server.mcp.list_tools()
    print(json.dumps(sorted(t.name for t in tools)))
asyncio.run(main())
"""
    env = os.environ.copy()
    env["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
    env["LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS"] = "true"
    env.pop("LLMLIBRARIAN_MCP_PROFILE", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    names = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "add_silo" in names
    assert "remove_file" in names


def test_write_scope_enforcement_filters_catalog(monkeypatch):
    """Read-scoped tokens must not see write-tagged tools when writes are enabled."""
    from fastmcp.server.auth.authorization import AuthContext
    from fastmcp.server.auth import restrict_tag
    from types import SimpleNamespace

    check = restrict_tag("write", scopes=["write"])
    write_tool = SimpleNamespace(tags={"write"}, name="add_silo")
    read_tool = SimpleNamespace(tags=set(), name="health")

    read_token = SimpleNamespace(scopes=["read"])
    write_token = SimpleNamespace(scopes=["read", "write"])

    assert check(AuthContext(token=read_token, component=read_tool)) is True
    assert check(AuthContext(token=read_token, component=write_tool)) is False
    assert check(AuthContext(token=write_token, component=write_tool)) is True
    assert check(AuthContext(token=None, component=write_tool)) is False


def test_healthz_stays_open_without_auth(monkeypatch):
    from starlette.testclient import TestClient

    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "127.0.0.1")
    with TestClient(mcp_server.mcp.http_app(path="/mcp")) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_v1_capabilities_requires_auth_when_enabled(monkeypatch):
    from starlette.testclient import TestClient

    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "cap-tok")
    with TestClient(mcp_server.mcp.http_app(path="/mcp")) as client:
        denied = client.get("/v1/capabilities")
        allowed = client.get(
            "/v1/capabilities", headers={"Authorization": "Bearer cap-tok"}
        )
    assert denied.status_code == 401
    assert denied.json()["api"] == "error/v1"
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["name"] == "llmlibrarian"
    assert body["auth"]["required"] is True
    assert body["bind"]["public_exposure_supported"] is False
    assert body["write_tools_enabled"] is False
    tool_names = {t["name"] for t in body["tools"]}
    assert "llmlibrarian.health" in tool_names
    assert "llmlibrarian.add_silo" not in tool_names


def test_rate_limiter_returns_retry_after():
    import mcp_http_security as sec

    limiter = sec.SlidingWindowRateLimiter(limit=2, window_s=60.0)
    assert limiter.check("tok:a", now=100.0) is None
    assert limiter.check("tok:a", now=100.1) is None
    retry = limiter.check("tok:a", now=100.2)
    assert retry is not None and retry >= 1.0


def test_rate_limit_key_ignores_unverified_bearer_tokens():
    import mcp_http_security as sec

    # Spoofed bearer must collapse to the client IP, not mint token:<unique>.
    assert (
        sec.rate_limit_key_from_authorization(
            "Bearer spoofed-1",
            "10.0.0.9",
            known_tokens={"real-token"},
        )
        == "ip:10.0.0.9"
    )
    assert (
        sec.rate_limit_key_from_authorization(
            "Bearer real-token",
            "10.0.0.9",
            known_tokens={"real-token"},
        )
        == "token:real-token"
    )


def test_rate_limiter_bounds_and_evicts_idle_keys():
    import mcp_http_security as sec

    limiter = sec.SlidingWindowRateLimiter(limit=5, window_s=10.0, max_keys=2)
    assert limiter.check("a", now=1.0) is None
    assert limiter.check("b", now=2.0) is None
    assert limiter.check("c", now=3.0) is None  # evicts oldest (a)
    keys = limiter.known_keys()
    assert "c" in keys
    assert len(keys) <= 2
    # After the window, idle buckets disappear.
    assert limiter.check("c", now=20.0) is None
    assert limiter.known_keys() == {"c"}


def test_rate_limit_middleware_emits_error_v1(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    import mcp_http_security as sec
    import mcp_server

    async def ok(_request):
        return PlainTextResponse("ok")

    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "r1")
    limiters = sec.CostClassRateLimiters(
        read_cheap=sec.SlidingWindowRateLimiter(limit=1, window_s=60.0),
        read_heavy=sec.SlidingWindowRateLimiter(limit=10, window_s=60.0),
    )
    app = mcp_server._HttpRateLimitMiddleware(
        Starlette(routes=[Route("/mcp", ok)]), limiters
    )
    with TestClient(app) as client:
        first = client.get("/mcp", headers={"Authorization": "Bearer r1"})
        second = client.get("/mcp", headers={"Authorization": "Bearer r1"})
        # Distinct spoofed bearers must share the IP bucket, not bypass it.
        spoof_a = client.get("/mcp", headers={"Authorization": "Bearer spoof-a"})
        spoof_b = client.get("/mcp", headers={"Authorization": "Bearer spoof-b"})
        health = client.get("/healthz")
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers.get("Retry-After")
    body = second.json()
    assert body["api"] == "error/v1"
    assert body["error"]["code"] == "rate_limited"
    assert spoof_a.status_code == 200  # first hit on ip:testclient
    assert spoof_b.status_code == 429
    # /healthz is exempt even when the route is missing on this stub app —
    # middleware must not 429 it; Starlette returns 404 which is fine.
    assert health.status_code in {200, 404}


def test_rate_limit_middleware_enforces_read_heavy_tool_class(monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    import mcp_http_security as sec
    import mcp_server

    async def ok(_request):
        return PlainTextResponse("ok")

    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "heavy-tok")
    limiters = sec.CostClassRateLimiters(
        read_cheap=sec.SlidingWindowRateLimiter(limit=60, window_s=60.0),
        read_heavy=sec.SlidingWindowRateLimiter(limit=2, window_s=60.0),
    )
    app = mcp_server._HttpRateLimitMiddleware(
        Starlette(routes=[Route("/mcp", ok, methods=["POST"])]), limiters
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "query_personal_knowledge", "arguments": {"query": "x"}},
    }
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer heavy-tok"}
        assert client.post("/mcp", headers=headers, json=payload).status_code == 200
        assert client.post("/mcp", headers=headers, json=payload).status_code == 200
        limited = client.post("/mcp", headers=headers, json=payload)
        # A cheap tools/list must not share the heavy bucket.
        cheap = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert limited.status_code == 429
    assert "read_heavy" in limited.json()["error"]["message"]
    assert cheap.status_code == 200


def test_refuse_write_tools_without_auth(monkeypatch):
    import mcp_http_security as sec

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", "127.0.0.1")
    with pytest.raises(RuntimeError, match="ENABLE_WRITE_TOOLS=true requires"):
        sec.validate_http_security_config("127.0.0.1")


def test_refuse_auth_off_on_non_loopback(monkeypatch):
    import mcp_http_security as sec

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "false")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS", "false")
    with pytest.raises(RuntimeError, match="only allowed when"):
        sec.validate_http_security_config("0.0.0.0")
    with pytest.raises(RuntimeError, match="only allowed when"):
        sec.validate_http_security_config("192.168.1.10")
    # Loopback + auth off + writes off is the trusted local escape hatch.
    sec.validate_http_security_config("127.0.0.1")


def test_token_compare_is_constant_time_helper():
    import mcp_http_security as sec

    callers = [
        sec.CallerToken("a", "alpha-token-value-0001", ("read",)),
        sec.CallerToken("b", "beta-token-value-00002", ("read", "write")),
    ]
    assert sec.find_caller_by_token("beta-token-value-00002", callers).client_id == "b"
    assert sec.find_caller_by_token("nope", callers) is None
    assert sec.constant_time_token_match("alpha-token-value-0001", [c.token for c in callers])


def test_env_example_auth_tokens_json_is_shell_safe():
    """Bash `source` must preserve JSON; wrap the value in single quotes."""
    text = (ROOT / ".env.mcp.example").read_text(encoding="utf-8")
    assert "LLMLIBRARIAN_MCP_AUTH_TOKENS='" in text
    assert 'LLMLIBRARIAN_MCP_AUTH_TOKENS={"hermes"' not in text.replace("# ", "")


def test_select_client_bearer_prefers_write_token():
    import mcp_http_security as sec

    tok = sec.select_client_bearer_token(
        {
            "LLMLIBRARIAN_MCP_AUTH_TOKENS": json.dumps(
                {
                    "hermes": {"token": "read-only", "scopes": ["read"]},
                    "pal": {"token": "ops-write", "scopes": ["read", "write"]},
                }
            )
        }
    )
    assert tok == "ops-write"
