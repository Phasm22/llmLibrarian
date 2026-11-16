#!/usr/bin/env bash
# Build llmLibrarian.app (native SwiftUI status app) and optionally install it.
#
#   macos/build.sh            → macos/build/llmLibrarian.app
#   macos/build.sh --install  → also replaces /Applications/llmLibrarian.app
#   macos/build.sh --install --relaunch → install and reopen the app
#
# The bundle keeps the two launcher shims in Contents/Resources that the launchd
# plists exec (llmlibrarian-mcp, llmlibrarian-chroma); they point at this checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
APP="$HERE/build/llmLibrarian.app"
INSTALL_DIR="${LLMLIBRARIAN_APP_DIR:-/Applications}"
INSTALL=false
RELAUNCH=false
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=true ;;
    --relaunch) RELAUNCH=true ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
if $RELAUNCH && ! $INSTALL; then
  echo "--relaunch requires --install" >&2
  exit 2
fi

cd "$HERE"
# Capture provenance before compiling, so About describes this build rather
# than whichever commit the checkout happens to contain when the app opens.
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_BRANCH="$(git -C "$REPO" symbolic-ref --short -q HEAD 2>/dev/null || echo detached)"
BUILD_STATE=unknown
if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BUILD_STATE=clean
  [[ -z "$(git -C "$REPO" status --porcelain --untracked-files=normal)" ]] || BUILD_STATE=modified
fi
echo "==> swift build (release)"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/llmLibrarian"
[[ -x "$BIN" ]] || { echo "build failed: $BIN missing" >&2; exit 1; }

echo "==> assembling bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/llmLibrarian"
cp "$HERE/Resources/Info.plist" "$APP/Contents/Info.plist"
# Release version is tracked in Resources/Info.plist. Build numbers advance
# locally, surviving build/ cleanup and never going below the installed app.
COUNTER="$HERE/.build/app-build-number"
mkdir -p "$HERE/.build"
BUILD_NUMBER=2
for previous in "$(cat "$COUNTER" 2>/dev/null || true)" \
                "$(plutil -extract CFBundleVersion raw -o - "$INSTALL_DIR/llmLibrarian.app/Contents/Info.plist" 2>/dev/null || true)"; do
  if [[ "$previous" =~ ^[0-9]{1,9}$ ]] && (( 10#$previous > BUILD_NUMBER )); then
    BUILD_NUMBER=$((10#$previous))
  fi
done
BUILD_NUMBER=$((BUILD_NUMBER + 1))
plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "$APP/Contents/Info.plist"
plutil -insert LibrarianBuildDate -string "$BUILD_DATE" "$APP/Contents/Info.plist"
plutil -insert LibrarianBuildCommit -string "$BUILD_COMMIT" "$APP/Contents/Info.plist"
plutil -insert LibrarianBuildBranch -string "$BUILD_BRANCH" "$APP/Contents/Info.plist"
plutil -insert LibrarianBuildState -string "$BUILD_STATE" "$APP/Contents/Info.plist"
plutil -insert LibrarianBuildSource -string "$REPO" "$APP/Contents/Info.plist"

ICONSET="$HERE/build/AppIcon.iconset"
ICON="$HERE/build/AppIcon.icns"
if [[ ! -s "$ICON" || "$HERE/Tools/make-icon.swift" -nt "$ICON" ]]; then
  echo "==> rendering icon"
  rm -rf "$ICONSET"
  swift "$HERE/Tools/make-icon.swift" "$ICONSET" >/dev/null
  iconutil -c icns "$ICONSET" -o "$HERE/build/AppIcon.new.icns"
  mv "$HERE/build/AppIcon.new.icns" "$ICON"
fi
cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"

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
codesign --force --deep --sign - "$APP"
printf '%s\n' "$BUILD_NUMBER" > "$COUNTER.tmp"
mv "$COUNTER.tmp" "$COUNTER"
echo "==> build number $BUILD_NUMBER"

echo "built: $APP"

if $INSTALL; then
  echo "==> installing to $INSTALL_DIR"
  staged="$INSTALL_DIR/llmLibrarian.app.new"
  rm -rf "$staged"
  ditto "$APP" "$staged"
  # Stage successfully before closing the GUI. Services also use the process
  # name llmLibrarian, so match only the GUI's full executable path.
  # -a includes our parent app: macOS pkill normally skips ancestors, which
  # otherwise leaves an old GUI running alongside the newly launched build.
  pkill -a -x -f "$INSTALL_DIR/llmLibrarian.app/Contents/MacOS/llmLibrarian" 2>/dev/null || true
  rm -rf "$INSTALL_DIR/llmLibrarian.app"
  mv "$staged" "$INSTALL_DIR/llmLibrarian.app"
  echo "installed: $INSTALL_DIR/llmLibrarian.app"
  if $RELAUNCH; then
    open -n "$INSTALL_DIR/llmLibrarian.app"
  fi
fi
echo "==> finished in ${SECONDS}s"
