#!/usr/bin/env bash
set -euo pipefail

# cc-nerf-buster install script
# Builds, installs binary, generates CA, prints env vars to source.

BINARY_NAME="cc-nerf-buster"
INSTALL_DIR="$HOME/.local/bin"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/cc-nerf-buster}"
PORT=9480

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }

# --- Build & install ---
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"
go build -o "$BINARY_NAME" .
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
cp "$BINARY_NAME" "$INSTALL_DIR/$BINARY_NAME"
chmod +x "$INSTALL_DIR/$BINARY_NAME"
green "Installed to $INSTALL_DIR/$BINARY_NAME"

# --- Generate CA ---
CA_CERT="$DATA_DIR/ca.crt"
if [ ! -f "$CA_CERT" ]; then
    "$INSTALL_DIR/$BINARY_NAME" --data-dir "$DATA_DIR" --init-ca
    [ -f "$CA_CERT" ] || { red "CA generation failed"; exit 1; }
fi
green "CA: $CA_CERT"

# --- Print env block ---
echo ""
cat <<EOF
# cc-nerf-buster (SSL inspection)
export https_proxy="http://localhost:$PORT"
export HTTPS_PROXY="http://localhost:$PORT"
export http_proxy="http://localhost:$PORT"
export HTTP_PROXY="http://localhost:$PORT"
export NODE_EXTRA_CA_CERTS="$CA_CERT"
export SSL_CERT_FILE="$CA_CERT"
export CURL_CA_BUNDLE="$CA_CERT"
export REQUESTS_CA_BUNDLE="$CA_CERT"
export GIT_SSL_CAINFO="$CA_CERT"
EOF
