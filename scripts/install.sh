#!/usr/bin/env bash
# scripts/install.sh — Bootstrap llmLibrarian + pal
#
# Usage (from a local clone):
#   bash scripts/install.sh
#
# Usage (remote, once published):
#   bash <(curl -fsSL https://raw.githubusercontent.com/YOUR_ORG/llmLibrarian/main/scripts/install.sh)
#
# Environment overrides:
#   LLMLIBRARIAN_REPO_URL   Git URL for remote install (default: placeholder)
#   LLMLIBRARIAN_DB         Custom DB path forwarded to pal install
#   LLMLIBRARIAN_MODEL      Custom model name forwarded to pal install
#   INSTALL_MCP=1           Also install the MCP HTTP service

set -euo pipefail

REPO_URL="${LLMLIBRARIAN_REPO_URL:-https://github.com/YOUR_ORG/llmLibrarian.git}"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. Locate repo root (works when run from inside a clone) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/../pal.py" ]]; then
    REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    LOCAL_INSTALL=1
else
    LOCAL_INSTALL=0
    REPO_DIR=""
fi

# ── 2. Check Python 3.10+ ─────────────────────────────────────────────────────
info "Checking Python version..."
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        major="${ver%%.*}"
        minor="${ver#*.}"
        if (( major > PYTHON_MIN_MAJOR || (major == PYTHON_MIN_MAJOR && minor >= PYTHON_MIN_MINOR) )); then
            PYTHON="$candidate"
            ok "Found $candidate $ver"
            break
        else
            warn "$candidate $ver is below 3.10 — skipping"
        fi
    fi
done
[[ -n "$PYTHON" ]] || die "Python 3.10+ not found. Install it from https://python.org and re-run."

OS="$(uname -s)"

# ── 3. Prefer pipx; fall back to pip --user ───────────────────────────────────
USE_PIPX=0
if command -v pipx &>/dev/null; then
    USE_PIPX=1
    ok "pipx already installed ($(pipx --version))"
else
    info "pipx not found — attempting to install..."
    if [[ "$OS" == "Darwin" ]] && command -v brew &>/dev/null; then
        brew install pipx
        pipx ensurepath
        USE_PIPX=1
    else
        "$PYTHON" -m pip install --user --quiet pipx \
            && "$PYTHON" -m pipx ensurepath \
            && USE_PIPX=1 \
            || warn "Could not install pipx — will fall back to pip --user"
    fi
fi

# pipx may have landed in ~/.local/bin which isn't in PATH yet
if (( USE_PIPX )) && ! command -v pipx &>/dev/null; then
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── 4. Install the package ────────────────────────────────────────────────────
info "Installing llmLibrarian..."
if (( USE_PIPX )); then
    if (( LOCAL_INSTALL )); then
        pipx install --force "$REPO_DIR"
    else
        pipx install --force "git+${REPO_URL}"
    fi
    ok "Installed via pipx"
else
    warn "Falling back to pip --user (no venv isolation)"
    if (( LOCAL_INSTALL )); then
        "$PYTHON" -m pip install --user --quiet "$REPO_DIR"
    else
        "$PYTHON" -m pip install --user --quiet "git+${REPO_URL}"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    ok "Installed via pip --user"
fi

# ── 5. Verify pal is on PATH ──────────────────────────────────────────────────
if ! command -v pal &>/dev/null; then
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v pal &>/dev/null || die "'pal' not found after install. Check that ~/.local/bin is in your PATH."
ok "pal found at $(command -v pal)"

# ── 6. Run pal install ────────────────────────────────────────────────────────
info "Running pal install..."
PAL_ARGS=()
[[ -n "${LLMLIBRARIAN_DB:-}" ]]    && PAL_ARGS+=("--db"    "$LLMLIBRARIAN_DB")
[[ -n "${LLMLIBRARIAN_MODEL:-}" ]] && PAL_ARGS+=("--model" "$LLMLIBRARIAN_MODEL")
[[ "${INSTALL_MCP:-0}" == "1" ]]   && PAL_ARGS+=("--mcp")

pal install "${PAL_ARGS[@]}"

# ── 7. PATH reminder ──────────────────────────────────────────────────────────
echo ""
if (( USE_PIPX )); then
    echo "If 'pal' is not found in new terminals, add this to your shell profile (~/.bashrc or ~/.zshrc):"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
