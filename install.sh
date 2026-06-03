#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/hhist"
TARGET_DIR="${HOME}/.local/bin"
TARGET="$TARGET_DIR/hhist"

if [[ ! -f "$SRC" ]]; then
  echo "hhist not found: $SRC" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"
install -m 755 "$SRC" "$TARGET"

echo "Installed: $TARGET"
echo ""
echo "If ~/.local/bin is not in PATH, add this line to ~/.bashrc or ~/.zshrc:"
echo 'export PATH="$HOME/.local/bin:$PATH"'
echo ""
echo "Try:"
echo "  hhist --list"
