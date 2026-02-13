# Security and Testing (Current)

This is the concise, maintained security/testing reference.

## Documentation Map

- Agent workflow truth: `AGENTS.md`
- User/operator usage: `README.md`
- Runtime behavior contracts: `docs/TECH.md`

## Security Posture (Local-Only)

- Threat model: single-user local CLI.
- Retrieved context is treated as untrusted evidence.
- Sensitive path/file exclusions are enforced in ingest defaults.
- Watch stop logic includes safety checks (ownership/signature checks where available).

## Known Operational Risks

- Local filesystem permissions still matter for DB and logs.
- Trace files can contain sensitive query text if enabled.

## Testing Policy

Before merging behavior changes:
```bash
uv run pytest -q tests/unit
```

For focused changes, run the relevant subset first, then full unit suite.

## What Not to Store Here

- Point-in-time coverage percentages
- Long historical findings tables
- One-off audit narrative that drifts over time

Use git history for historical audit details.
