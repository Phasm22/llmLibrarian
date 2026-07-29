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

# NOTE: deliberately NOT using the renamed-interpreter trick here (see
# run_mcp_http.sh / pal.py's watch daemons for that). Running Chroma's server
# through the manually-copied .venv/bin/llmLibrarian + .venv/lib/libpython
# combo SIGSEGV'd under launchd (crash-looped) even though it worked fine
# interactively — not worth the risk on the single process that owns the
# on-disk index. This process shows as "Python" in Activity Monitor; that's
# an acceptable tradeoff for the most critical service in the stack.
if [[ -x "$ROOT_DIR/.venv/bin/chroma" ]]; then
  CHROMA_BIN="$ROOT_DIR/.venv/bin/chroma"
elif command -v chroma >/dev/null 2>&1; then
  CHROMA_BIN="$(command -v chroma)"
else
  echo "chroma CLI not found. Install deps: uv sync (provides chromadb CLI)." >&2
  exit 127
fi

exec "$CHROMA_BIN" run \
  --path "$DB_PATH" \
  --host "$HOST" \
  --port "$PORT"
