# Security and Testing (Current)

This is the concise, maintained security/testing reference.

## Documentation Map

- Agent workflow truth: `AGENTS.md`
- User/operator usage: `README.md`
- Runtime behavior contracts: `docs/TECH.md`

## Security Posture (Local-Only)

- Threat model: single-user local CLI, **plus** an optional MCP HTTP service.
- The MCP HTTP service listens on a socket. On loopback with no auth it is
  reachable by any local process. Off-loopback (including
  `scripts/publish_mcp_funnel.sh`) it **must** run with
  `LLMLIBRARIAN_MCP_REQUIRE_AUTH=true` and a token from `openssl rand -hex 32`.
- `/healthz` is unauthenticated by design (supervisors probe it) and therefore
  withholds `db_path` when the server is exposed without auth.
- MCP write tools (`add_silo`, `trigger_reindex`, `repair_silo`, `update_file`,
  `remove_file`) require `confirm=True`. That is a deliberate-action guard, not
  an authorization boundary: anyone who can call the server can pass it.
  `add_silo` reads an arbitrary filesystem path, so treat the token as the only
  real boundary.
- Retrieved context is treated as untrusted evidence.
- Sensitive path/file exclusions are enforced in ingest defaults.
- Watch stop logic includes safety checks (ownership/signature checks where available).

## Known Operational Risks

- Local filesystem permissions still matter for DB and logs.
- Trace files can contain sensitive query text if enabled.

## Testing Policy

Before merging behavior changes:
```bash
uv run pytest            # tests/unit + tests/contract (see pyproject)
uv run pytest tests/integration   # spawns subprocesses + real Chroma
```

For focused changes, run the relevant subset first, then full unit suite.

## What Not to Store Here

- Point-in-time coverage percentages
- Long historical findings tables
- One-off audit narrative that drifts over time

Use git history for historical audit details.
