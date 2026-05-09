#!/usr/bin/env bash
# One-time Mac setup to route all Claude Code API traffic through the
# cc-nerf-buster transparent proxy running on the homelab runner VM.
#
# Safe to re-run — all mutations are idempotent.

set -euo pipefail

PROXY_HOST="192.168.7.208"
REMOTE_CA="deploy@${PROXY_HOST}:/opt/nomad-volumes/cc-nerf-buster/ca.crt"
LOCAL_CA="${HOME}/cc-nerf-buster-ca.crt"
HOSTS_ENTRY="${PROXY_HOST}  api.anthropic.com"
ZSHRC="${HOME}/.zshrc"
NODE_CA_LINE="export NODE_EXTRA_CA_CERTS=${LOCAL_CA}"

echo "==> Fetching CA cert from proxy VM..."
scp "${REMOTE_CA}" "${LOCAL_CA}"
echo "    Saved to ${LOCAL_CA}"

echo "==> Trusting CA in macOS system keychain..."
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain "${LOCAL_CA}"
echo "    Trusted."

echo "==> Adding NODE_EXTRA_CA_CERTS to ${ZSHRC}..."
if grep -qF "${NODE_CA_LINE}" "${ZSHRC}" 2>/dev/null; then
  echo "    Already present — skipping."
else
  echo "${NODE_CA_LINE}" >> "${ZSHRC}"
  echo "    Added."
fi

echo "==> Adding api.anthropic.com to /etc/hosts..."
if grep -qF "${HOSTS_ENTRY}" /etc/hosts 2>/dev/null; then
  echo "    Already present — skipping."
else
  echo "${HOSTS_ENTRY}" | sudo tee -a /etc/hosts > /dev/null
  echo "    Added."
fi

echo ""
echo "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Reload your shell:  source ${ZSHRC}"
echo "  2. Verify the proxy cert is serving:"
echo "     curl -sv https://api.anthropic.com 2>&1 | grep -E 'SSL|issuer|subject|Connected'"
echo "     You should see 'issuer: O=cc-nerf-buster' in the output."
