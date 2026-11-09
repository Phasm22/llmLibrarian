# Security and Testing (Current)

This is the concise, maintained security/testing reference.

## Documentation Map

- Agent workflow truth: `AGENTS.md`
- User/operator usage: `README.md`
- Runtime behavior contracts: `docs/TECH.md`
- HTTP MCP exposure: `docs/MCP_TAILSCALE_FUNNEL.md`

## Security Posture (Local-Only)

- Threat model: single-user local CLI, **plus** an optional MCP HTTP service.
- That service listens on a socket. **Default bind is `127.0.0.1`.** Off-loopback
  bind is explicit for LAN/tailnet only. **Public exposure (Tailscale Funnel /
  port forwards) is not supported.** A URL path prefix is routing only — not a
  security control.
- **Auth fails closed.** `LLMLIBRARIAN_MCP_REQUIRE_AUTH` defaults to `true`.
  HTTP MCP refuses to start if no `LLMLIBRARIAN_MCP_AUTH_TOKEN` /
  `LLMLIBRARIAN_MCP_AUTH_TOKENS` is configured. `/healthz` stays open without
  a token. Generate tokens with `openssl rand -hex 32`; never commit them.
- Tokens are per-caller and scoped `read` or `write`. Write MCP tools
  (`add_silo`, `trigger_reindex`, `repair_silo`, `update_file`, `remove_file`,
  `set_silo_privacy`) are **off by default**; enable with
  `LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS=true` **and** a token that carries the
  `write` scope.
- MCP write tools still require `confirm=True`. That makes a destructive call
  deliberate; it is not an authorization boundary. The bearer scope is.
- Rate limits return HTTP 429 with `Retry-After` and an `error/v1` body.
  `GET /v1/capabilities` lists tools and advertised limits (auth required when
  auth is on).
- Retrieved context is treated as untrusted evidence.
- Sensitive path/file exclusions are enforced in ingest defaults.
- Watch stop logic includes safety checks (ownership/signature checks where available).

## Known Operational Risks

- Local filesystem permissions still matter for DB and logs.
- Trace files can contain sensitive query text if enabled.

## Testing Policy

Before merging behavior changes:
```bash
uv run pytest -q tests/unit tests/contract
```

For focused changes, run the relevant subset first, then full unit suite.

## What Not to Store Here

- Point-in-time coverage percentages
- Long historical findings tables
- One-off audit narrative that drifts over time

Use git history for historical audit details.
