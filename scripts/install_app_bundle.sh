#!/usr/bin/env bash
# Install /Applications/llmLibrarian.app — a minimal app bundle that anchors
# the launchd background items in System Settings.
#
# macOS 13+ shows LaunchAgents in Login Items & Extensions. Bare shell-script
# agents appear as "run_chroma_server.sh — Item from unidentified developer."
# Giving every agent plist an AssociatedBundleIdentifiers entry pointing at
# this bundle groups them under one "llmLibrarian" item with a real name.
# The bundle is ad-hoc signed: free, local, sufficient for name attribution.
# (Only a paid Apple Developer ID identity would replace the "unidentified
# developer" wording itself.)
#
# The bundle also carries per-service launchers (llmlibrarian-chroma,
# llmlibrarian-mcp) that exec the repo's run scripts. Pointing the launchd
# plists' program at these paths is what actually makes System Settings
# attribute the items to this app: attribution follows the executable's
# containing bundle, while the exec-chain still ends in the same python
# process as before (so runtime behavior and TCC identity are unchanged).
#
# Double-clicking the app shows a status dialog of the loaded services.
#
# Usage: install_app_bundle.sh [repo_dir]   (default: this script's repo)
set -euo pipefail

APP="/Applications/llmLibrarian.app"
BUNDLE_ID="com.llmlibrarian.app"
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${1:-$(cd "$_script_dir/.." && pwd)}"

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleName</key>
  <string>llmLibrarian</string>
  <key>CFBundleDisplayName</key>
  <string>llmLibrarian</string>
  <key>CFBundleExecutable</key>
  <string>llmLibrarian</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>0.1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>LSUIElement</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/llmLibrarian" <<'STUB'
#!/bin/bash
# Status dialog for the llmLibrarian background services.
summary="$(launchctl list 2>/dev/null | awk '/llmlibrarian/ {
  status = ($1 == "-") ? "stopped" : "pid " $1
  printf "%s  —  %s\n", $3, status
}')"
[ -z "$summary" ] && summary="No llmLibrarian services loaded."
osascript -e 'on run argv
  display dialog (item 1 of argv) with title "llmLibrarian services" buttons {"OK"} default button 1 giving up after 60
end run' "$summary" >/dev/null 2>&1 || true
STUB
chmod +x "$APP/Contents/MacOS/llmLibrarian"

# Launchers live in Resources/, not MacOS/: codesign refuses extra
# non-Mach-O executables in MacOS/ (nested code must carry its own
# signature, which a shell script cannot), while Resources/ entries are
# sealed as plain resources. Bundle attribution in System Settings follows
# path containment, so Resources/ works the same for that purpose.
mkdir -p "$APP/Contents/Resources"
for svc in chroma mcp; do
  case "$svc" in
    chroma) target="$REPO_DIR/scripts/run_chroma_server.sh" ;;
    mcp)    target="$REPO_DIR/scripts/run_mcp_http.sh" ;;
  esac
  cat > "$APP/Contents/Resources/llmlibrarian-$svc" <<LAUNCHER
#!/bin/bash
exec "$target" "\$@"
LAUNCHER
  chmod +x "$APP/Contents/Resources/llmlibrarian-$svc"
done

codesign --force -s - "$APP"
echo "Installed and ad-hoc signed $APP (bundle id ${BUNDLE_ID}, repo ${REPO_DIR})"
