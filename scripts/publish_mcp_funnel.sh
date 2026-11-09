#!/usr/bin/env bash
# Historical helper. Public Funnel exposure of llmLibrarian MCP is not supported.
# See docs/MCP_TAILSCALE_FUNNEL.md.
set -euo pipefail

echo "Refusing to publish llmLibrarian MCP via Tailscale Funnel." >&2
echo "Public exposure is not supported; bind 127.0.0.1 with REQUIRE_AUTH=true." >&2
echo "If Funnel is already enabled, run: tailscale funnel reset" >&2
echo "Details: docs/MCP_TAILSCALE_FUNNEL.md" >&2
exit 1
