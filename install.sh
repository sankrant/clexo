#!/usr/bin/env bash
# clexo installer — installs the package into an isolated environment (pipx/uv,
# falling back to pip --user) and wires it into Claude Code via `clexo install`.
# Re-runnable; `clexo install` is idempotent and re-points any older install.
#
# Prefer the standard path directly:
#     pipx install git+https://github.com/sankrant/clexo
#     clexo install
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v pipx >/dev/null 2>&1; then
  echo "▶ Installing clexo with pipx (isolated venv)…"
  pipx install --force "$SCRIPT_DIR"
elif command -v uv >/dev/null 2>&1; then
  echo "▶ Installing clexo with uv tool…"
  uv tool install --force "$SCRIPT_DIR"
else
  echo "▶ pipx/uv not found — falling back to: pip install --user"
  echo "  (recommended: install pipx — e.g. 'brew install pipx' or 'python3 -m pip install --user pipx')"
  python3 -m pip install --user --force-reinstall "$SCRIPT_DIR"
fi

if ! command -v clexo >/dev/null 2>&1; then
  echo
  echo "⚠ 'clexo' is not on your PATH yet."
  echo "  pipx: run 'pipx ensurepath' and open a new shell, then: clexo install"
  echo "  uv:   ensure ~/.local/bin is on PATH, then: clexo install"
  exit 0
fi

echo
exec clexo install
