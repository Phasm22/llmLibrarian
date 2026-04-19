# MCP over Tailscale Funnel

This runbook exposes `mcp_server.py` over HTTPS for ChatGPT while keeping the MCP process bound to localhost.

## 1) Prepare runtime env

```bash
cp .env.mcp.example .env.mcp
```

Defaults in `.env.mcp.example`:
- `LLMLIBRARIAN_MCP_TRANSPORT=streamable-http`
- `LLMLIBRARIAN_MCP_HOST=127.0.0.1`
- `LLMLIBRARIAN_MCP_PORT=8765`
- `LLMLIBRARIAN_MCP_PATH=/mcp`
- `LLMLIBRARIAN_MCP_REQUIRE_AUTH=false` (recommended for Tailscale-only)

If you want static Bearer auth later:

```bash
openssl rand -hex 32
# set LLMLIBRARIAN_MCP_REQUIRE_AUTH=true
# paste token into LLMLIBRARIAN_MCP_AUTH_TOKEN
```

## 2) Run MCP as a local HTTP service

```bash
scripts/run_mcp_http.sh
```

Local checks:

```bash
curl -sS http://127.0.0.1:8765/healthz
curl -i -X POST http://127.0.0.1:8765/mcp
# expected: auth depends on LLMLIBRARIAN_MCP_REQUIRE_AUTH
```

## 3) Persist service

### macOS (launchd)

1. Copy `deploy/launchd/com.tjm4.llmlibrarian-mcp.plist` to `~/Library/LaunchAgents/`.
2. Update any hardcoded paths/usernames.
3. Load service:

```bash
launchctl unload ~/Library/LaunchAgents/com.tjm4.llmlibrarian-mcp.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.tjm4.llmlibrarian-mcp.plist
launchctl kickstart -k gui/$(id -u)/com.tjm4.llmlibrarian-mcp
```

### Linux (systemd)

Use `deploy/systemd/llmlibrarian-mcp.service` as a template unit.

## 4) Publish via Tailscale Funnel

```bash
scripts/publish_mcp_funnel.sh 8765
```

If Funnel is not enabled on your tailnet, Tailscale prints an admin URL. Enable Funnel there, then rerun.

Get the public URL:

```bash
tailscale funnel status
```

## 5) Configure ChatGPT MCP connector

Use:
- URL: `https://<funnel-domain>/mcp`
- Auth: `None` (for Tailscale/Funnel-only setup)
- Optional hardened mode: Bearer token from `LLMLIBRARIAN_MCP_AUTH_TOKEN`

Smoke-test expected behavior:
1. Connector lists tools.
2. `capabilities` returns supported file types.
3. `health` returns `db_path` and storage info.

## 6) Key rotation

1. Generate a new token (`openssl rand -hex 32`).
2. Update `LLMLIBRARIAN_MCP_AUTH_TOKEN` in `.env.mcp`.
3. Restart service (`launchctl kickstart -k ...` or `systemctl restart ...`).
4. Update token in ChatGPT connector.

## 7) Exposure controls and rollback

- Stop public exposure only:

```bash
tailscale funnel reset
```

- Stop service:
  - macOS: `launchctl unload ~/Library/LaunchAgents/com.tjm4.llmlibrarian-mcp.plist`
  - Linux: `sudo systemctl stop llmlibrarian-mcp`

- Full rollback to local-only MCP integration:
  - Keep using `.mcp.json` stdio entry (`python mcp_server.py`).

## 8) Hardening notes

- Keep MCP bound to localhost (`127.0.0.1`) and publish only through Funnel.
- Never commit `.env.mcp`.
- Prefer read-only tools first in new remote clients (`capabilities`, `health`, `list_silos`).
- Treat `repair_silo`, `trigger_reindex`, and `add_silo` as privileged operations.
