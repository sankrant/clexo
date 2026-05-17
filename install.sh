#!/usr/bin/env bash
# clexo installer — checks requirements, installs deps, wires up the MCP server
# and the `clexo` CLI. Re-runnable; existing setup is left alone unless you
# confirm a replacement.
set -euo pipefail

# Resolve script dir even when invoked via a symlink
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
SERVER="$SCRIPT_DIR/server.py"
WRAPPER="$SCRIPT_DIR/clexo"
REQS="$SCRIPT_DIR/requirements.txt"

# Colors (only if stdout is a tty)
if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
  C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi

step() { printf "\n%s▶ %s%s\n" "$C_BOLD" "$1" "$C_OFF"; }
ok()   { printf "  %s✓%s %s\n" "$C_GREEN"  "$C_OFF" "$1"; }
warn() { printf "  %s⚠%s %s\n" "$C_YELLOW" "$C_OFF" "$1"; }
err()  { printf "  %s✗%s %s\n" "$C_RED"    "$C_OFF" "$1" >&2; }
fail() { err "$1"; exit 1; }
ask()  { local reply; read -r -p "  $1 [y/N] " reply || true; [[ "$reply" =~ ^[Yy] ]]; }

printf "%sclexo installer%s  %s(source: %s)%s\n" \
  "$C_BOLD" "$C_OFF" "$C_DIM" "$SCRIPT_DIR" "$C_OFF"

# ── 1. Python ≥ 3.10 ──────────────────────────────────────────────────────────
step "Checking Python"
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found on PATH. Install Python 3.10 or newer first."
fi
PYV=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  fail "Python $PYV is too old. clexo needs Python 3.10+."
fi
PYBIN=$(command -v python3)
ok "python3 = $PYV ($PYBIN)"

# ── 2. mcp package ────────────────────────────────────────────────────────────
step "Installing Python dependencies"
if python3 -c 'import mcp' >/dev/null 2>&1; then
  ok "mcp package already importable"
else
  if python3 -m pip install --quiet -r "$REQS"; then
    ok "installed: $(tr '\n' ' ' < "$REQS")"
  else
    warn "pip install failed. Try manually:"
    echo "    python3 -m pip install -r $REQS"
    ask "Continue without it?" || fail "Aborted."
  fi
fi

# ── 3. Register MCP server with Claude Code ──────────────────────────────────
step "Registering MCP server with Claude Code"
if ! command -v claude >/dev/null 2>&1; then
  warn "claude CLI not found on PATH — skipping MCP registration."
  echo "    After installing the Claude CLI, run:"
  echo "    ${C_DIM}claude mcp add --scope user clexo python3 $SERVER${C_OFF}"
else
  if claude mcp list 2>/dev/null | grep -qE '^[[:space:]]*clexo[[:space:]:]'; then
    warn "'clexo' is already registered as an MCP server."
    if ask "Re-add (will overwrite)?"; then
      claude mcp remove --scope user clexo >/dev/null 2>&1 || true
      claude mcp add --scope user clexo python3 "$SERVER" >/dev/null
      ok "re-registered → $SERVER"
    else
      ok "left existing registration in place"
    fi
  else
    claude mcp add --scope user clexo python3 "$SERVER" >/dev/null
    ok "registered as user-scope MCP server 'clexo' → $SERVER"
  fi
fi

# ── 4. CLI wrapper symlink ────────────────────────────────────────────────────
step "Linking the clexo CLI"
BINDIR="${CLEXO_BINDIR:-$HOME/.local/bin}"
mkdir -p "$BINDIR"
TARGET="$BINDIR/clexo"
if [ -L "$TARGET" ] && [ "$(readlink "$TARGET" 2>/dev/null || true)" = "$WRAPPER" ]; then
  ok "already linked: $TARGET → $WRAPPER"
elif [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
  warn "$TARGET exists and doesn't point at our wrapper"
  if ask "Replace it?"; then
    rm -f "$TARGET"
    ln -s "$WRAPPER" "$TARGET"
    ok "linked $TARGET → $WRAPPER"
  fi
else
  ln -s "$WRAPPER" "$TARGET"
  ok "linked $TARGET → $WRAPPER"
fi

# PATH check
if ! printf ":%s:" "$PATH" | grep -q ":$BINDIR:"; then
  warn "$BINDIR is not on your PATH"
  case "${SHELL:-}" in
    */zsh)  RC="~/.zshrc"  ;;
    */bash) RC="~/.bashrc" ;;
    *)      RC="your shell rc file" ;;
  esac
  echo "    Add this to $RC:"
  echo "      ${C_DIM}export PATH=\"\$HOME/.local/bin:\$PATH\"${C_OFF}"
fi

# ── 5. Hook snippet (manual merge — JSON merging is risky) ───────────────────
step "Optional: SessionStart + SessionEnd hooks"
echo "  SessionStart auto-restores the last saved session after /clear."
echo "  SessionEnd keeps the FTS index fresh in the background."
echo "  Merge this into ~/.claude/settings.json (under \"hooks\"):"
echo
cat <<EOF
${C_DIM}    "SessionStart": [{
      "matcher": "clear",
      "hooks": [{
        "type": "command",
        "command": "python3 $SERVER --session-start"
      }]
    }],
    "SessionEnd": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "bash -c 'python3 $SERVER --sync >> /tmp/clexo-sync.log 2>&1 &'"
      }]
    }]${C_OFF}
EOF

echo
ok "${C_BOLD}Install complete.${C_OFF} Try: ${C_BOLD}clexo help${C_OFF}"
