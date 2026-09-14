# llmLibrarian.app (macOS)

A small native status app for the llmLibrarian background services. It replaces
the old AppleScript "services" dialog with a real window: an overview of index
health, the silo table, per-service controls (Chroma, MCP, folder watchers),
and the MCP query audit. It lives in the menu bar; the window opens on launch
or from the menu bar item.

Everything it shows is read directly from the same sources `pal ls --status`
uses — `~/.pal/daemon.json`, `<db>/llmli_registry.json`, `~/.pal/watch_locks`,
`launchctl list`, `/healthz`, Chroma's heartbeat, and
`~/.pal/logs/query-audit.jsonl` — so opening it never starts Python.
Actions shell out: launchctl for start/stop/restart, `pal pull` for reindex,
`pal daemon sync` for reconciling watchers.

## Build and install

```bash
macos/build.sh            # → macos/build/llmLibrarian.app
macos/build.sh --install  # also replaces /Applications/llmLibrarian.app
```

Requires the Xcode Command Line Tools (Swift 5.9+, macOS 14 SDK). The bundle
is ad-hoc signed. `Contents/Resources` keeps the two launcher shims that the
launchd plists exec (`llmlibrarian-mcp`, `llmlibrarian-chroma`); they point at
this checkout, so rebuild after moving the repo.

## Layout

- `Sources/llmLibrarian/Model` — `PalConfig` (paths and ports), data types, the observable `LibrarianStore`
- `Sources/llmLibrarian/Services` — `Collector` (state gathering), launchctl/ps wrappers, snapshot mode
- `Sources/llmLibrarian/Views` — Overview, Silos, Services, Activity, menu bar menu
- `Tools/make-icon.swift` — renders `AppIcon.icns` from an SF Symbol at build time

## Snapshot mode

Set `LLMLIBRARIAN_SNAPSHOT_DIR=/some/dir` when launching and the app writes a
PNG of every page in light and dark, then quits. Useful for checking layout
without screen-recording permission:

```bash
open -W --env LLMLIBRARIAN_SNAPSHOT_DIR=/tmp/shots macos/build/llmLibrarian.app
```
