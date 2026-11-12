#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_BIN="$HERE/.build/silo-add-workflow-tests"

mkdir -p "$HERE/.build"
swiftc \
  "$HERE/Sources/llmLibrarian/Model/SiloAddWorkflow.swift" \
  "$HERE/Tests/SiloAddWorkflowTests/main.swift" \
  -o "$TEST_BIN"
"$TEST_BIN"

ACTIVITY_BIN="$HERE/.build/watch-activity-tests"
swiftc \
  "$HERE/Sources/llmLibrarian/Model/Models.swift" \
  "$HERE/Sources/llmLibrarian/Model/Activity.swift" \
  "$HERE/Sources/llmLibrarian/Services/WatchActivity.swift" \
  "$HERE/Tests/WatchActivityTests/main.swift" \
  -o "$ACTIVITY_BIN"
"$ACTIVITY_BIN"

SAMPLE_BIN="$HERE/.build/sample-library-tests"
swiftc \
  "$HERE/Sources/llmLibrarian/Model/Models.swift" \
  "$HERE/Sources/llmLibrarian/Model/Config.swift" \
  "$HERE/Sources/llmLibrarian/Model/Activity.swift" \
  "$HERE/Sources/llmLibrarian/Services/ActivityLedger.swift" \
  "$HERE/Sources/llmLibrarian/Services/SampleLibrary.swift" \
  "$HERE/Tests/SampleLibraryTests/main.swift" \
  -lsqlite3 \
  -o "$SAMPLE_BIN"
"$SAMPLE_BIN"

LEDGER_BIN="$HERE/.build/activity-ledger-tests"
swiftc \
  "$HERE/Sources/llmLibrarian/Model/Models.swift" \
  "$HERE/Sources/llmLibrarian/Model/Activity.swift" \
  "$HERE/Sources/llmLibrarian/Services/ActivityLedger.swift" \
  "$HERE/Tests/ActivityLedgerTests/main.swift" \
  -lsqlite3 \
  -o "$LEDGER_BIN"
"$LEDGER_BIN" "$HERE/../tests/fixtures/activity"
