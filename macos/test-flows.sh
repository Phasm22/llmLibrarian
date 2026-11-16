#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/Sources/llmLibrarian"
mkdir -p "$HERE/.build"
swiftc "$SRC"/Model/*.swift \
  "$SRC/Services/Shell.swift" "$SRC/Services/Collector.swift" \
  "$SRC/Services/ActivityLedger.swift" "$SRC/Services/WatchActivity.swift" \
  "$SRC/Services/SampleLibrary.swift" "$SRC/Services/AppRebuild.swift" \
  "$SRC/Services/StoreRuntime.swift" "$HERE/Tests/FlowScenarioTests/Scenarios.swift" \
  -lsqlite3 -o "$HERE/.build/flow-scenario-tests"
"$HERE/.build/flow-scenario-tests"
