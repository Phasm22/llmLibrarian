#!/usr/bin/env bash
# Install llmlibrarian-mcp as a system service for the current user.
# Usage: sudo bash deploy/systemd/install.sh
set -euo pipefail

USER="${SUDO_USER:-$(id -un)}"
UNIT="llmlibrarian-mcp@${USER}.service"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/llmlibrarian-mcp@.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

cp "$SRC" /etc/systemd/system/llmlibrarian-mcp@.service
systemctl daemon-reload
systemctl enable --now "$UNIT"
echo "Installed and started $UNIT"
echo "  Check status: systemctl status $UNIT"
echo "  Logs:         journalctl -u $UNIT -f"
