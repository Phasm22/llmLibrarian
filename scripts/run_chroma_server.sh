#!/usr/bin/env bash
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$_script_dir/.." && pwd)"
if [[ ! -f "$ROOT_DIR/cli.py" ]]; then
  if [[ -n "${LLMLIBRARIAN_INSTALL_DIR:-}" && -f "${LLMLIBRARIAN_INSTALL_DIR}/cli.py" ]]; then
    ROOT_DIR="${LLMLIBRARIAN_INSTALL_DIR}"
  fi
fi

ENV_FILE="${LLMLI_CHROMA_ENV_FILE:-${LLMLI_MCP_ENV_FILE:-$ROOT_DIR/.env.mcp}}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB_PATH="${LLMLIBRARIAN_DB:-$ROOT_DIR/my_brain_db}"
HOST="${LLMLIBRARIAN_CHROMA_HOST:-127.0.0.1}"
PORT="${LLMLIBRARIAN_CHROMA_PORT:-8000}"

mkdir -p "$DB_PATH"

# Chroma server loads sentence-transformers from collection schema; pin CPU when
# the host has no GPU (indexed configs may still say cuda from another machine).
export LLMLIBRARIAN_EMBEDDING_DEVICE="${LLMLIBRARIAN_EMBEDDING_DEVICE:-cpu}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-}"

# Process title: run Chroma's CLI in-interpreter with setproctitle so ps/
# pgrep/lsof show "llmLibrarian-chroma:<port>" instead of a bare python path.
# This only rewrites argv — same interpreter, same chromadb package, no
# relocated binaries. (The 2026-07-28 crash-loop that made an earlier rename
# attempt look dangerous was later root-caused to on-disk HNSW corruption;
# the renamed-binary/copied-dylib experiment stays retired regardless —
# kernel p_comm reads "Python" and that's fine.)
PY_BIN="$ROOT_DIR/.venv/bin/python"
if [[ -x "$PY_BIN" ]]; then
  exec "$PY_BIN" -c '
import sys
try:
    import setproctitle
    port = sys.argv[sys.argv.index("--port") + 1]
    setproctitle.setproctitle(f"llmLibrarian-chroma:{port}")
except (ImportError, ValueError, IndexError):
    pass
from chromadb.cli.cli import app
app()
' run --path "$DB_PATH" --host "$HOST" --port "$PORT"
fi

# Fallback without the venv: plain console script, unnamed process.
if command -v chroma >/dev/null 2>&1; then
  exec "$(command -v chroma)" run \
    --path "$DB_PATH" \
    --host "$HOST" \
    --port "$PORT"
fi
echo "chroma CLI not found. Install deps: uv sync (provides chromadb CLI)." >&2
exit 127
