# Security and Testing (Current)

This is the concise, maintained security/testing reference.

## Documentation Map

- Agent workflow truth: `AGENTS.md`
- User/operator usage: `README.md`
- Runtime behavior contracts: `docs/TECH.md`

## Security Posture (Local-Only)

- Threat model: single-user local CLI, **plus** an optional MCP HTTP service.
- That service listens on a socket. On loopback without auth any local process
  can reach it; off-loopback (including `scripts/publish_mcp_funnel.sh`) it must
  run with `LLMLIBRARIAN_MCP_REQUIRE_AUTH=true` and a token from
  `openssl rand -hex 32`.
- MCP write tools require `confirm=True`. That makes a destructive call
  deliberate; it is not an authorization boundary, since any caller can pass it.
  `add_silo` indexes an arbitrary filesystem path, so the bearer token is the
  only real boundary.
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
