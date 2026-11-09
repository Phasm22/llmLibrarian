"""HTTP MCP security helpers: tokens, write-tool policy, rate limits, capabilities.

Implements the service-endpoint security bar for llmLibrarian's streamable-HTTP
MCP: fail-closed auth, read-default tool catalog, per-caller scopes, and a
small /v1/capabilities manifest. Secret URL paths are routing only — never a
security control. Public exposure (Tailscale Funnel / port forwards) is not
supported; bind 127.0.0.1 by default.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

# Tools that mutate the index / registry. Hidden unless explicitly enabled and
# the caller token carries the ``write`` scope.
WRITE_TOOL_NAMES: tuple[str, ...] = (
    "add_silo",
    "trigger_reindex",
    "repair_silo",
    "update_file",
    "remove_file",
    "set_silo_privacy",
)

READ_TOOL_NAMES: tuple[str, ...] = (
    "query_personal_knowledge",
    "multi_query_knowledge",
    "ask_image",
    "read_document",
    "recent_queries",
    "find_files",
    "explain_retrieval",
    "list_silos",
    "session_context",
    "inspect_silo",
    "watch_coverage",
    "mcp_runtime_status",
    "health",
    "capabilities",
)

# Cost classes advertised in /v1/capabilities (service-endpoint standard).
_RATE_LIMITS = {
    "read_cheap": {"per_caller_per_min": 60, "burst": 20},
    "read_heavy": {"per_caller_per_min": 10, "burst": 5},
}

_TOOL_COST_CLASS = {
    "query_personal_knowledge": "read_heavy",
    "multi_query_knowledge": "read_heavy",
    "ask_image": "read_heavy",
    "explain_retrieval": "read_heavy",
    "retrieve_knowledge": "read_heavy",
}


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def require_auth_enabled() -> bool:
    """HTTP MCP auth is on by default (fail closed)."""
    return env_bool("LLMLIBRARIAN_MCP_REQUIRE_AUTH", True)


def write_tools_enabled() -> bool:
    """Write MCP tools stay off unless the operator opts in."""
    return env_bool("LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS", False)


@dataclass(frozen=True)
class CallerToken:
    """One configured bearer token and its scopes."""

    client_id: str
    token: str
    scopes: tuple[str, ...]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _parse_scopes(raw: str | None, *, default: tuple[str, ...] = ("read",)) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return default
    scopes = tuple(
        part.strip().lower()
        for part in str(raw).replace(",", " ").split()
        if part.strip()
    )
    # Normalize aliases from the service-endpoint standard.
    normalized: list[str] = []
    for scope in scopes:
        if scope in {"propose", "write"}:
            if "write" not in normalized:
                normalized.append("write")
            if "read" not in normalized:
                # Write implies read so a write token can also list/query.
                normalized.insert(0, "read")
            continue
        if scope not in normalized:
            normalized.append(scope)
    return tuple(normalized) or default


def load_caller_tokens() -> list[CallerToken]:
    """Load per-caller bearer tokens from env.

    Preferred: ``LLMLIBRARIAN_MCP_AUTH_TOKENS`` JSON object::

        {"hermes": {"token": "...", "scopes": ["read"]},
         "pal": {"token": "...", "scopes": ["read", "write"]}}

    Fallback: single ``LLMLIBRARIAN_MCP_AUTH_TOKEN`` with scopes from
    ``LLMLIBRARIAN_MCP_AUTH_TOKEN_SCOPES`` (default ``read``).
    """
    callers: list[CallerToken] = []
    raw_map = os.environ.get("LLMLIBRARIAN_MCP_AUTH_TOKENS", "").strip()
    if raw_map:
        try:
            parsed = json.loads(raw_map)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LLMLIBRARIAN_MCP_AUTH_TOKENS must be a JSON object of "
                '{caller: {"token": "...", "scopes": ["read"]}}'
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError(
                "LLMLIBRARIAN_MCP_AUTH_TOKENS must be a non-empty JSON object"
            )
        for client_id, entry in parsed.items():
            if isinstance(entry, str):
                token = entry.strip()
                scopes = ("read",)
            elif isinstance(entry, dict):
                token = str(entry.get("token") or "").strip()
                scopes_val = entry.get("scopes", ["read"])
                if isinstance(scopes_val, str):
                    scopes = _parse_scopes(scopes_val)
                elif isinstance(scopes_val, list):
                    scopes = _parse_scopes(" ".join(str(s) for s in scopes_val))
                else:
                    scopes = ("read",)
            else:
                raise RuntimeError(
                    f"LLMLIBRARIAN_MCP_AUTH_TOKENS[{client_id!r}] must be a "
                    "token string or {token, scopes} object"
                )
            if not token:
                raise RuntimeError(
                    f"LLMLIBRARIAN_MCP_AUTH_TOKENS[{client_id!r}] has an empty token"
                )
            callers.append(CallerToken(str(client_id), token, scopes))

    single = os.environ.get("LLMLIBRARIAN_MCP_AUTH_TOKEN", "").strip()
    if single and not any(c.token == single for c in callers):
        scopes = _parse_scopes(os.environ.get("LLMLIBRARIAN_MCP_AUTH_TOKEN_SCOPES"))
        callers.append(CallerToken("default", single, scopes))

    return callers


def select_client_bearer_token(parsed_env: dict[str, str] | None = None) -> str | None:
    """Pick a bearer token for local clients (prefer write scope for pal ops)."""
    env = parsed_env if parsed_env is not None else os.environ
    # Explicit client override always wins.
    explicit = (env.get("LLMLIBRARIAN_MCP_BEARER_TOKEN") or "").strip()
    if explicit:
        return explicit

    raw_map = (env.get("LLMLIBRARIAN_MCP_AUTH_TOKENS") or "").strip()
    if raw_map:
        try:
            # Temporarily apply map through load_caller_tokens by swapping env
            # is awkward; parse locally.
            parsed = json.loads(raw_map)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            ranked: list[tuple[int, str]] = []
            for client_id, entry in parsed.items():
                if isinstance(entry, str):
                    token, scopes = entry.strip(), ("read",)
                elif isinstance(entry, dict):
                    token = str(entry.get("token") or "").strip()
                    scopes_val = entry.get("scopes", ["read"])
                    if isinstance(scopes_val, list):
                        scopes = _parse_scopes(" ".join(str(s) for s in scopes_val))
                    else:
                        scopes = _parse_scopes(str(scopes_val) if scopes_val else None)
                else:
                    continue
                if not token:
                    continue
                rank = 0 if "write" in scopes else 1
                ranked.append((rank, token))
            if ranked:
                ranked.sort()
                return ranked[0][1]

    single = (env.get("LLMLIBRARIAN_MCP_AUTH_TOKEN") or "").strip()
    return single or None


def auth_required_and_configured() -> list[CallerToken]:
    """Return configured tokens, or raise if auth is required but missing."""
    if not require_auth_enabled():
        return load_caller_tokens()
    callers = load_caller_tokens()
    if not callers:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH is enabled (default) but no token is "
            "configured. Set LLMLIBRARIAN_MCP_AUTH_TOKEN or "
            "LLMLIBRARIAN_MCP_AUTH_TOKENS (never commit real values), or set "
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH=false for trusted loopback-only testing."
        )
    return callers


class StaticBearerTokenAuth:
    """Multi-caller static bearer verifier returning scoped AccessTokens.

    Constructed lazily so mcp_server can subclass/wrap FastMCP's AuthProvider
    without importing heavy auth stacks at module import for every unit test.
    """

    def __init__(self, callers: Iterable[CallerToken], *, required_scopes: list[str] | None = None):
        self._by_token = {c.token: c for c in callers}
        if not self._by_token:
            raise RuntimeError("StaticBearerTokenAuth requires at least one token")
        self.required_scopes = required_scopes or []

    def lookup(self, token: str) -> CallerToken | None:
        return self._by_token.get(token)


class SlidingWindowRateLimiter:
    """Per-caller sliding window limiter. Returns retry_after_s when limited."""

    def __init__(self, *, limit: int = 60, window_s: float = 60.0):
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Return None if allowed, else seconds until the caller may retry."""
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window_s
        with self._lock:
            bucket = self._events.setdefault(key, [])
            # Drop expired.
            i = 0
            while i < len(bucket) and bucket[i] <= cutoff:
                i += 1
            if i:
                del bucket[:i]
            if len(bucket) >= self.limit:
                retry_after = max(1.0, self.window_s - (ts - bucket[0]))
                return retry_after
            bucket.append(ts)
            return None


def rate_limit_key_from_authorization(authorization: str | None, client_host: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return f"token:{token}"
    host = (client_host or "").strip() or "unknown"
    return f"ip:{host}"


def error_v1(
    *,
    code: str,
    message: str,
    http_status: int,
    retryable: bool = False,
    retry_after_s: float | int | None = None,
    hint: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> tuple[dict[str, Any], int, dict[str, str]]:
    """Build an error/v1 body, status, and extra headers."""
    rid = request_id or f"r-{uuid.uuid4().hex[:8]}"
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    headers: dict[str, str] = {"X-Request-Id": rid}
    if retry_after_s is not None:
        retry_int = max(1, int(round(float(retry_after_s))))
        err["retry_after_s"] = retry_int
        headers["Retry-After"] = str(retry_int)
    if hint:
        err["hint"] = hint
    body = {
        "api": "error/v1",
        "request_id": rid,
        "trace_id": trace_id or rid,
        "error": err,
    }
    return body, http_status, headers


def capabilities_manifest(
    *,
    version: str,
    tool_names: Iterable[str],
    write_tools_enabled_flag: bool,
    require_auth: bool,
    host: str,
    rate_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Service manifest for GET /v1/capabilities."""
    tools = []
    for name in sorted(tool_names):
        access = "write" if name in WRITE_TOOL_NAMES else "read"
        cost = _TOOL_COST_CLASS.get(name, "read_cheap")
        limit = (rate_limits or _RATE_LIMITS).get(cost, _RATE_LIMITS["read_cheap"])
        tools.append(
            {
                "name": f"llmlibrarian.{name}",
                "access": access,
                "cost_class": cost,
                "rate_limit": {
                    "per_caller_per_min": limit["per_caller_per_min"],
                },
            }
        )
    return {
        "name": "llmlibrarian",
        "version": version,
        "owner": "llmLibrarian",
        "availability": "always",
        "auth": {
            "required": require_auth,
            "schemes": ["bearer"],
            "scopes": ["read", "write"],
        },
        "bind": {
            "host": host,
            "public_exposure_supported": False,
            "note": (
                "Default bind is 127.0.0.1. LAN/tailnet bind is explicit. "
                "Tailscale Funnel / public port forwards are not supported. "
                "URL path prefixes are routing only, not a security control."
            ),
        },
        "write_tools_enabled": write_tools_enabled_flag,
        "datasets": [{"name": "knowledge_silos", "max_age_s": None}],
        "rate_limits": rate_limits or _RATE_LIMITS,
        "tools": tools,
    }


def default_rate_limiter() -> SlidingWindowRateLimiter:
    cheap = _RATE_LIMITS["read_cheap"]["per_caller_per_min"]
    return SlidingWindowRateLimiter(limit=int(cheap), window_s=60.0)
