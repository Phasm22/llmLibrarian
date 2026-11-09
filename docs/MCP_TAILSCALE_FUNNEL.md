# MCP HTTP exposure (Funnel not supported)

Public exposure of llmLibrarian MCP via Tailscale Funnel (or any port forward)
is **not supported**. The inventory that produced the service-endpoint standard
found this path was previously guarded only by an obscured URL — that is not a
security control.

## Supported topology

1. Bind **127.0.0.1** (default).
2. Keep **`LLMLIBRARIAN_MCP_REQUIRE_AUTH=true`** (default) with a real token in
   `.env.mcp` (mode 600; never commit).
3. Leave write tools off unless an operator token needs them:
   `LLMLIBRARIAN_MCP_ENABLE_WRITE_TOOLS=true` **and** a token scoped `write`.
4. Optional LAN/tailnet bind is explicit (`LLMLIBRARIAN_MCP_HOST=…`) and still
   requires bearer auth. Phone/public access waits for a separate Tailscale redo.

`LLMLIBRARIAN_MCP_PATH` remains a mount route for clients; do not treat a secret
path as protection.

## Local HTTP service

```bash
cp .env.mcp.example .env.mcp
openssl rand -hex 32   # paste into LLMLIBRARIAN_MCP_AUTH_TOKEN
# edit .env.mcp — REQUIRE_AUTH=true, HOST=127.0.0.1, ENABLE_WRITE_TOOLS as needed
scripts/run_mcp_http.sh
```

Checks:

```bash
curl -sS http://127.0.0.1:8765/healthz
curl -sS -H "Authorization: Bearer $LLMLIBRARIAN_MCP_AUTH_TOKEN" \
  http://127.0.0.1:8765/v1/capabilities
```

## Historical Funnel script

`scripts/publish_mcp_funnel.sh` remains in the tree only as a rollback artifact.
Do not use it for new deploys. If Funnel is still enabled on a host, turn it off:

```bash
tailscale funnel reset
```

Point MCP clients at `http://127.0.0.1:$LLMLIBRARIAN_MCP_PORT$LLMLIBRARIAN_MCP_PATH`
with `Authorization: Bearer …`. Do **not** reintroduce a stdio MCP entry for
remote clients — it spawns a second process per client against the same Chroma path.
