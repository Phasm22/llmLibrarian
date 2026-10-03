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
