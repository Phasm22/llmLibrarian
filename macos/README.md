# llmLibrarian.app (macOS)

A small native status app for the llmLibrarian background services. It replaces
the old AppleScript "services" dialog with a real window: an overview of index
health, the silo table, per-service controls (Chroma, MCP, folder watchers),
and the MCP query audit. It lives in the menu bar; the window opens on launch
or from the menu bar item.

Everything it shows is read directly from the same sources `pal ls --status`
uses — `~/.pal/daemon.json`, `<db>/llmli_registry.json`, `~/.pal/watch_locks`,
`launchctl list`, `/healthz`, Chroma's heartbeat,
`~/.pal/logs/query-audit.jsonl`, and the activity ledger
`<db>/llmli_activity.sqlite3` (read-only, via the system SQLite) — so opening it
never starts Python.

The Overview answers four things over Today / 7 days / 30 days: what needs
action (one strip, only when something does), three numbers with trends
(documents changed, searches, searches that found no sources), the busiest
collection, and one row per collection. Every number clicks through to the
Activity page (Searches / Changes / Problems), which reads the rows on demand.
`Services/ActivityLedger.swift` runs the same SQL as `activity_ledger.SUMMARY_SQL`
against the ledger's views; `tests/fixtures/activity` pins both readers to the
same output. Design: [docs/plans/overview-information-design.md](../docs/plans/overview-information-design.md).
Actions shell out: launchctl for start/stop/restart, `pal pull` for adding and
reindexing silos, and `pal daemon sync` for reconciling watchers. Add Silo is
available from the Silos toolbar, the empty state, and File → Add Silo….
Finder also exposes Add as llmLibrarian Silo when one or more folders are
selected and the contextual Services menu is opened. Both entry points show an
image-vision opt-in before indexing. Existing silos with image vision off expose
Enable Image Vision in their details and context menu; enabling it reindexes
with `pal pull <folder> --image-vision`.

## Build and install

```bash
macos/build.sh            # → macos/build/llmLibrarian.app
macos/build.sh --install  # also replaces /Applications/llmLibrarian.app
macos/build.sh --install --relaunch  # install and reopen
```

In the app, choose **llmLibrarian → Rebuild and Relaunch**, or the same action
from the menu bar status icon. It builds the current local checkout in the
background, stages the new bundle, then closes and reopens the app. The toolbar
shows progress; **Open Build Log** opens `~/.pal/logs/app-build.log` (under
`PAL_HOME` when customized). Build failures appear in the app with a log link.
The action is disabled while a rebuild or indexing job is running and in sample mode.
It uses the configured daemon checkout and the same install destination as the
script (`/Applications` by default). It does not pull Git changes, run the Python
test suite, or restart background services. Swift retains its build cache, and
the icon is regenerated only when its generator changes or the cached icon is missing.

The native app release version is tracked in `Resources/Info.plist` independently
of the Python package version. Each successful bundle build increments
`.build/app-build-number`, using the installed app's build number as a floor.
About shows that number plus the build time, source commit, branch, and whether
the checkout contained local edits. Version `0.2.0 (2)` had been fixed since the
native app's September 14, 2026 introduction; the recovery-flow changes are `0.3.0`.

Requires the Xcode Command Line Tools (Swift 5.9+, macOS 14 SDK). The bundle
is ad-hoc signed. `Contents/Resources` keeps the two launcher shims that the
launchd plists exec (`llmlibrarian-mcp`, `llmlibrarian-chroma`); they point at
this checkout, so rebuild after moving the repo.

Run the framework-free workflow tests with `macos/test.sh`. They compile the
cross-platform Add Silo command planner, the watcher-log parser, the sample
library, and the activity-ledger reader directly, so XCTest is not required. The
ledger test reads the shared fixture; after changing the overview contract,
regenerate it with `TZ=UTC uv run python tests/fixtures/activity/make_fixture.py`.

`macos/test.sh` also runs `bash macos/test-flows.sh`: the production store is
tested with real subprocesses and temporary directories, with injected service
responses instead of live launchd or index writes. See
[UI recovery checks](../docs/macos-ui-recovery-checks.md) for the scenario matrix
and the remaining boundaries. Snapshot mode includes Activity Changes/Problems,
About, and the empty Jobs panel in light and dark appearances.

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

## Native menus and toolbar

- **File:** Add Silo (⇧⌘O), reveal the selected silo in Finder (⇧⌘R), or open the selected service log.
- **Edit:** native text editing, Copy Selected Item (⇧⌘C), and Copy MCP Endpoint.
- **View:** persistent Show Toolbar Labels (compact secondary captions), sidebar, pages (⌘1–4), details (⌥⌘I), zoom (⌘+/⌘−/⌘0), and refresh (⌘R).
- **Silo:** reindex, privacy, and image vision for the selected silo, using the same confirmations as its detail view.
- **Window:** Show llmLibrarian (⇧⌘L), plus native window management.
- **Help:** guide (⌘?), setup, CLI reference, and the GitHub issue form.

Selection-dependent actions disable when they do not apply. Native Cut/Copy/Paste
continue to target the focused text field. The toolbar provides Add Silo and Refresh
on every page; the service-status buttons navigate to the matching details.
Closing the window leaves the menu bar app running; Window → Show llmLibrarian reopens it.

## Public screenshots

Never publish the default snapshot output without reviewing it: it contains live
folder names, paths, and query history. Use the isolated sample library for public assets:

```bash
macos/build.sh
open -n -W \
  --env LLMLIBRARIAN_SNAPSHOT_DIR=/tmp/llmlibrarian-public-shots \
  --env LLMLIBRARIAN_SNAPSHOT_SAMPLE=1 \
  macos/build/llmLibrarian.app
```

Both environment variables are required for sample mode. It bypasses live collection,
uses synthetic records under `/tmp/llmlibrarian-sample`, and blocks subprocess/service
actions. The title identifies it as a sample library. The PNGs are captures of the
actual SwiftUI views, not mockups. Review them visually before copying selected images
into `docs/images/`. Sample counts are illustrative, not performance claims.
