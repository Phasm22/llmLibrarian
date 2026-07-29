#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LLMLI_MCP_ENV_FILE:-$ROOT_DIR/.env.mcp}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  echo "Copy .env.mcp.example to .env.mcp." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export LLMLIBRARIAN_MCP_TRANSPORT="${LLMLIBRARIAN_MCP_TRANSPORT:-streamable-http}"
export LLMLIBRARIAN_MCP_HOST="${LLMLIBRARIAN_MCP_HOST:-127.0.0.1}"
export LLMLIBRARIAN_MCP_PORT="${LLMLIBRARIAN_MCP_PORT:-8765}"
export LLMLIBRARIAN_MCP_PATH="${LLMLIBRARIAN_MCP_PATH:-/mcp}"
export LLMLIBRARIAN_MCP_STATELESS_HTTP="${LLMLIBRARIAN_MCP_STATELESS_HTTP:-true}"
export LLMLIBRARIAN_MCP_LOG_LEVEL="${LLMLIBRARIAN_MCP_LOG_LEVEL:-warning}"
export LLMLIBRARIAN_MCP_REQUIRE_AUTH="${LLMLIBRARIAN_MCP_REQUIRE_AUTH:-false}"

require_auth_lc="$(printf '%s' "$LLMLIBRARIAN_MCP_REQUIRE_AUTH" | tr '[:upper:]' '[:lower:]')"
if [[ "$require_auth_lc" == "true" ]] && [[ -z "${LLMLIBRARIAN_MCP_AUTH_TOKEN:-}" ]]; then
  echo "LLMLIBRARIAN_MCP_AUTH_TOKEN is required when LLMLIBRARIAN_MCP_REQUIRE_AUTH=true." >&2
  exit 1
fi

# Prefer our renamed interpreter (see pal.py's _daemon_runtime_metadata) so
# this shows as "llmLibrarian" instead of "Python" in Activity Monitor/top.
# Falls back to the plain venv python if the rename step hasn't run yet.
if [[ -x "$ROOT_DIR/.venv/bin/llmLibrarian" ]]; then
  exec "$ROOT_DIR/.venv/bin/llmLibrarian" "$ROOT_DIR/mcp_server.py"
else
  exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/mcp_server.py"
fi
