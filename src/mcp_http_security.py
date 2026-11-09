"""HTTP MCP security helpers: tokens, write-tool policy, rate limits, capabilities.

Implements the service-endpoint security bar for llmLibrarian's streamable-HTTP
MCP: fail-closed auth, read-default tool catalog, per-caller scopes, and a
small /v1/capabilities manifest. Secret URL paths are routing only — never a
security control. Public exposure (Tailscale Funnel / port forwards) is not
supported; bind 127.0.0.1 by default.
"""

from __future__ import annotations

import hmac
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

# Cost classes advertised in /v1/capabilities and enforced by the HTTP middleware.
RATE_LIMITS = {
    "read_cheap": {"per_caller_per_min": 60, "burst": 20},
    "read_heavy": {"per_caller_per_min": 10, "burst": 5},
}
_RATE_LIMITS = RATE_LIMITS  # backward-compatible alias for callers/tests

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


def is_loopback_bind(host: str) -> bool:
    """True for loopback-only bind addresses (trusted local testing)."""
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


def validate_http_security_config(host: str | None = None) -> None:
    """Refuse unsafe HTTP combinations before the server binds.

    - Write tools require auth (scope middleware is a no-op when auth is off).
    - Auth may only be disabled on a loopback bind.
    """
    bind_host = (host if host is not None else os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1"))
    bind_host = (bind_host or "127.0.0.1").strip()
    auth_on = require_auth_enabled()
    writes_on = write_tools_enabled()
    if writes_on and not auth_on:
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS=true requires "
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH=true. Write tools without auth would "
            "be exposed to every caller that can reach the socket."
        )
    if not auth_on and not is_loopback_bind(bind_host):
        raise RuntimeError(
            "LLMLIBRARIAN_MCP_REQUIRE_AUTH=false is only allowed when "
            f"LLMLIBRARIAN_MCP_HOST is loopback (got {bind_host!r}). "
            "Use 127.0.0.1/localhost/::1 for trusted local testing, or keep auth on."
        )


def constant_time_token_match(candidate: str, known_tokens: Iterable[str]) -> bool:
    """True if candidate equals any known token (hmac.compare_digest)."""
    matched = False
    cand = candidate or ""
    for token in known_tokens:
        # Always run the compare so timing does not short-circuit on the first hit.
        if hmac.compare_digest(cand, token or ""):
            matched = True
    return matched


def find_caller_by_token(candidate: str, callers: Iterable[CallerToken]) -> CallerToken | None:
    """Return the matching CallerToken using constant-time compares."""
    matched: CallerToken | None = None
    cand = candidate or ""
    for caller in callers:
        if hmac.compare_digest(cand, caller.token or ""):
            matched = caller
    return matched


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
    Lookup uses hmac.compare_digest against every configured token.
    """

    def __init__(self, callers: Iterable[CallerToken], *, required_scopes: list[str] | None = None):
        self._callers = list(callers)
        if not self._callers:
            raise RuntimeError("StaticBearerTokenAuth requires at least one token")
        self.required_scopes = required_scopes or []

    def lookup(self, token: str) -> CallerToken | None:
        return find_caller_by_token(token, self._callers)


class SlidingWindowRateLimiter:
    """Per-caller sliding window limiter. Returns retry_after_s when limited.

    Idle buckets are dropped once their window expires. ``max_keys`` caps how
    many distinct caller keys can be retained so a flood of unique identities
    cannot grow memory without bound.
    """

    def __init__(
        self,
        *,
        limit: int = 60,
        window_s: float = 60.0,
        max_keys: int = 1024,
    ):
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self.max_keys = max(1, int(max_keys))
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune_bucket(self, bucket: list[float], cutoff: float) -> None:
        i = 0
        while i < len(bucket) and bucket[i] <= cutoff:
            i += 1
        if i:
            del bucket[:i]

    def _evict_idle(self, cutoff: float) -> None:
        stale = [key for key, bucket in self._events.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            self._events.pop(key, None)

    def check(self, key: str, *, now: float | None = None) -> float | None:
        """Return None if allowed, else seconds until the caller may retry."""
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window_s
        with self._lock:
            self._evict_idle(cutoff)
            bucket = self._events.get(key)
            if bucket is None:
                if len(self._events) >= self.max_keys:
                    # Prefer dropping the oldest idle/least-recent key rather
                    # than allocating unbounded maps under spoofed identities.
                    oldest_key = min(
                        self._events.items(),
                        key=lambda item: item[1][-1] if item[1] else 0.0,
                    )[0]
                    self._events.pop(oldest_key, None)
                bucket = []
                self._events[key] = bucket
            else:
                self._prune_bucket(bucket, cutoff)
            if len(bucket) >= self.limit:
                retry_after = max(1.0, self.window_s - (ts - bucket[0]))
                return retry_after
            bucket.append(ts)
            return None

    def known_keys(self) -> set[str]:
        with self._lock:
            return set(self._events)


def rate_limit_key_from_authorization(
    authorization: str | None,
    client_host: str | None,
    *,
    known_tokens: Iterable[str] | None = None,
) -> str:
    """Build a limiter key.

    Only **verified** bearer tokens become ``token:…`` keys. Arbitrary /
    unknown Authorization values fall back to the client host so failed-auth
    floods cannot mint unbounded buckets or bypass the IP throttle.
    """
    known = list(known_tokens or ())
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token and constant_time_token_match(token, known):
            return f"token:{token}"
    host = (client_host or "").strip() or "unknown"
    return f"ip:{host}"


def tool_cost_class(tool_name: str | None) -> str:
    """Cost class for a tool name (default read_cheap)."""
    if not tool_name:
        return "read_cheap"
    return _TOOL_COST_CLASS.get(tool_name, "read_cheap")


def mcp_tool_name_from_jsonrpc(body: bytes | str | None) -> str | None:
    """Best-effort tool name from a JSON-RPC tools/call body."""
    if not body:
        return None
    try:
        raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("method") != "tools/call":
        return None
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return str(name) if name else None


@dataclass
class CostClassRateLimiters:
    """Separate sliding windows for read_cheap vs read_heavy (as advertised)."""

    read_cheap: SlidingWindowRateLimiter
    read_heavy: SlidingWindowRateLimiter

    def check(self, key: str, cost_class: str, *, now: float | None = None) -> float | None:
        if cost_class == "read_heavy":
            return self.read_heavy.check(key, now=now)
        return self.read_cheap.check(key, now=now)


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
    """Legacy single-window helper (read_cheap). Prefer default_cost_class_limiters()."""
    cheap = _RATE_LIMITS["read_cheap"]["per_caller_per_min"]
    return SlidingWindowRateLimiter(limit=int(cheap), window_s=60.0)


def default_cost_class_limiters() -> CostClassRateLimiters:
    """Limiters matching the advertised /v1/capabilities rate_limits table."""
    return CostClassRateLimiters(
        read_cheap=SlidingWindowRateLimiter(
            limit=int(_RATE_LIMITS["read_cheap"]["per_caller_per_min"]),
            window_s=60.0,
        ),
        read_heavy=SlidingWindowRateLimiter(
            limit=int(_RATE_LIMITS["read_heavy"]["per_caller_per_min"]),
            window_s=60.0,
        ),
    )
