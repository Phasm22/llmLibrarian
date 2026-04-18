#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8765}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale CLI not found." >&2
  exit 1
fi

echo "Publishing local port $PORT with Tailscale Funnel..."
if ! tailscale funnel --bg "$PORT"; then
  echo
  echo "Funnel could not be enabled automatically." >&2
  echo "If prompted, enable Funnel in the Tailscale admin UI and rerun this script." >&2
  exit 1
fi

echo
echo "Current Funnel config:"
tailscale funnel status
