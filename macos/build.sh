#!/usr/bin/env bash
# Build llmLibrarian.app (native SwiftUI status app) and optionally install it.
#
#   macos/build.sh            → macos/build/llmLibrarian.app
#   macos/build.sh --install  → also replaces /Applications/llmLibrarian.app
#
# The bundle keeps the two launcher shims in Contents/Resources that the launchd
# plists exec (llmlibrarian-mcp, llmlibrarian-chroma); they point at this checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
APP="$HERE/build/llmLibrarian.app"
INSTALL_DIR="${LLMLIBRARIAN_APP_DIR:-/Applications}"

cd "$HERE"
echo "==> swift build (release)"
swift build -c release 2>&1 | grep -v '^\['
[[ ${PIPESTATUS[0]} -eq 0 ]] || { echo "swift build failed" >&2; exit 1; }
BIN="$(swift build -c release --show-bin-path)/llmLibrarian"
[[ -x "$BIN" ]] || { echo "build failed: $BIN missing" >&2; exit 1; }

echo "==> assembling bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/llmLibrarian"
cp "$HERE/Resources/Info.plist" "$APP/Contents/Info.plist"

echo "==> rendering icon"
ICONSET="$HERE/build/AppIcon.iconset"
rm -rf "$ICONSET"
swift "$HERE/Tools/make-icon.swift" "$ICONSET" >/dev/null
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "==> service launcher shims"
for pair in "llmlibrarian-mcp:run_mcp_http.sh" "llmlibrarian-chroma:run_chroma_server.sh"; do
  shim="${pair%%:*}"; script="${pair##*:}"
  cat > "$APP/Contents/Resources/$shim" <<SH
#!/bin/bash
exec "$REPO/scripts/$script" "\$@"
SH
  chmod +x "$APP/Contents/Resources/$shim"
done

echo "==> codesign (ad hoc)"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1

echo "built: $APP"

if [[ "${1:-}" == "--install" ]]; then
  echo "==> installing to $INSTALL_DIR"
  # Only the GUI app; the services run a python renamed "llmLibrarian" too.
  pkill -f "$INSTALL_DIR/llmLibrarian.app/Contents/MacOS/llmLibrarian" 2>/dev/null || true
  staged="$INSTALL_DIR/llmLibrarian.app.new"
  rm -rf "$staged"
  ditto "$APP" "$staged"
  rm -rf "$INSTALL_DIR/llmLibrarian.app"
  mv "$staged" "$INSTALL_DIR/llmLibrarian.app"
  echo "installed: $INSTALL_DIR/llmLibrarian.app"
fi
