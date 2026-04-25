#!/usr/bin/env bash
set -euo pipefail

BINARY_NAME="cc-nerf-buster"
INSTALL_DIR="$HOME/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/cc-nerf-buster}"

green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$*"; }

if [ -f "$INSTALL_DIR/$BINARY_NAME" ]; then
    rm "$INSTALL_DIR/$BINARY_NAME"
    green "Removed $INSTALL_DIR/$BINARY_NAME"
else
    dim "Binary not found"
fi

echo ""
dim "Data directory preserved: $DATA_DIR"
dim "Remove manually if no longer needed: rm -rf $DATA_DIR"
dim "Remove cc-nerf-buster block from your shell profile (~/.zshrc or ~/.bashrc)"
green "Uninstalled."
