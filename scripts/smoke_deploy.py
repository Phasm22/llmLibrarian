#!/usr/bin/env python3
"""Post-deploy smoke test for a live llmLibrarian MCP HTTP server.

Usage (token comes from the environment; it is never printed):

    export LLMLI_SMOKE_TOKEN=...   # the server's bearer token
    uv run python scripts/smoke_deploy.py http://127.0.0.1:8765/mcp
    uv run python scripts/smoke_deploy.py https://host/mcp --query "tax notes"

Exit status is 0 only when every check passes:

  1. GET /healthz returns 200 and JSON ``ok`` is true.
  2. POST /mcp with no credentials returns 401.
  3. An authenticated initialize + tools/list returns a non-empty tool list.
  4. An authenticated, unscoped query_personal_knowledge returns at least one chunk.

The query is unscoped on purpose: private silos are skipped, so the smoke run
never reads a private corpus. Only counts are printed, never chunk text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TOKEN_ENV = "LLMLI_SMOKE_TOKEN"
DEFAULT_QUERY = "overview"
JSON_RPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


class SmokeFailure(Exception):
    """A check that did not pass. The message must never contain the token."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def healthz_url(mcp_url: str) -> str:
    """/healthz lives at the origin, beside the MCP mount."""
    parts = urlsplit(mcp_url)
    return urlunsplit((parts.scheme, parts.netloc, "/healthz", "", ""))


def _parse_mcp_body(response: httpx.Response) -> dict:
    """JSON-RPC body from either a plain JSON response or an SSE stream."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [line[len("data:"):].strip() for line in response.text.splitlines() if line.startswith("data:")]
        if not events:
            raise SmokeFailure("empty event stream from MCP endpoint")
        return json.loads(events[-1])
    return response.json()


class McpSession:
    """Minimal streamable-HTTP MCP client: initialize, notify, request."""

    def __init__(self, client: httpx.Client, url: str, token: str) -> None:
        self._client = client
        self._url = url
        self._token = token
        self._session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {
            **JSON_RPC_HEADERS,
            "Authorization": f"Bearer {self._token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _post(self, payload: dict) -> httpx.Response:
        response = self._client.post(self._url, json=payload, headers=self._headers())
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return response

    def request(self, method: str, params: dict | None = None) -> dict:
        message: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        self._next_id += 1
        if params is not None:
            message["params"] = params
        response = self._post(message)
        if response.status_code != 200:
            raise SmokeFailure(f"{method} returned HTTP {response.status_code}")
        body = _parse_mcp_body(response)
        if "error" in body:
            code = (body.get("error") or {}).get("code")
            raise SmokeFailure(f"{method} returned JSON-RPC error {code}")
        return body.get("result") or {}

    def notify(self, method: str) -> None:
        response = self._post({"jsonrpc": "2.0", "method": method})
        if response.status_code >= 400:
            raise SmokeFailure(f"{method} returned HTTP {response.status_code}")

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "smoke-deploy", "version": "1"},
            },
        )
        self.notify("notifications/initialized")


def check_healthz(client: httpx.Client, mcp_url: str) -> CheckResult:
    name = "healthz ok"
    try:
        response = client.get(healthz_url(mcp_url))
        if response.status_code != 200:
            raise SmokeFailure(f"/healthz returned HTTP {response.status_code}")
        body = response.json()
        if body.get("ok") is not True:
            raise SmokeFailure("/healthz reports ok is not true")
    except (httpx.HTTPError, ValueError, SmokeFailure) as exc:
        return CheckResult(name, False, _describe(exc))
    detail = f"version {body.get('version')}, image_backend {body.get('image_backend')}"
    return CheckResult(name, True, detail)


def check_unauthenticated_rejected(client: httpx.Client, mcp_url: str) -> CheckResult:
    name = "unauthenticated MCP call -> 401"
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "smoke-deploy", "version": "1"},
        },
    }
    try:
        response = client.post(mcp_url, json=message, headers=JSON_RPC_HEADERS)
    except httpx.HTTPError as exc:
        return CheckResult(name, False, _describe(exc))
    if response.status_code != 401:
        return CheckResult(name, False, f"got HTTP {response.status_code}, expected 401")
    return CheckResult(name, True, "HTTP 401")


def check_tools_list(session: McpSession) -> CheckResult:
    name = "authenticated tools/list"
    try:
        session.initialize()
        tools = session.request("tools/list").get("tools") or []
        if not tools:
            raise SmokeFailure("tools/list returned no tools")
    except (httpx.HTTPError, ValueError, SmokeFailure) as exc:
        return CheckResult(name, False, _describe(exc))
    return CheckResult(name, True, f"{len(tools)} tools")


def check_search(session: McpSession, query: str) -> CheckResult:
    name = "search returns results"
    try:
        result = session.request(
            "tools/call",
            {"name": "query_personal_knowledge", "arguments": {"query": query, "n_results": 3}},
        )
        if result.get("isError"):
            raise SmokeFailure("query_personal_knowledge reported an error")
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(result["content"][0]["text"])
        if payload.get("error"):
            raise SmokeFailure("query_personal_knowledge returned an error field")
        chunks = payload.get("chunks") or []
        if not chunks:
            raise SmokeFailure("query returned zero chunks")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, SmokeFailure) as exc:
        return CheckResult(name, False, _describe(exc))
    return CheckResult(name, True, f"{len(chunks)} chunk(s)")


def _describe(exc: Exception) -> str:
    """Error text for the report. Exception classes only: no response bodies, no headers."""
    if isinstance(exc, SmokeFailure):
        return str(exc)
    return f"{type(exc).__name__} while contacting the server"


def run_checks(client: httpx.Client, mcp_url: str, token: str, query: str) -> list[CheckResult]:
    """Run all four checks. Every check runs even if an earlier one fails."""
    session = McpSession(client, mcp_url, token)
    results = [
        check_healthz(client, mcp_url),
        check_unauthenticated_rejected(client, mcp_url),
        check_tools_list(session),
    ]
    results.append(check_search(session, query))
    return results


def _report(results: list[CheckResult], out) -> None:
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}  {result.name}: {result.detail}", file=out)


def main(
    argv: list[str] | None = None,
    *,
    make_client: Callable[[float], httpx.Client] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="MCP endpoint, e.g. https://host/mcp")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                        help=f"env var holding the bearer token (default {DEFAULT_TOKEN_ENV})")
    parser.add_argument("--query", default=os.environ.get("LLMLI_SMOKE_QUERY", DEFAULT_QUERY),
                        help="small unscoped search query (default: $LLMLI_SMOKE_QUERY or 'overview')")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"error: {args.token_env} is not set; export the server's bearer token there.",
              file=sys.stderr)
        return 2

    build = make_client or (lambda timeout: httpx.Client(timeout=timeout))
    with build(args.timeout) as client:
        results = run_checks(client, args.url, token, args.query)
    _report(results, sys.stdout)
    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"smoke FAILED: {len(failed)} of {len(results)} checks failed", file=sys.stderr)
        return 1
    print(f"smoke passed: {len(results)} of {len(results)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
