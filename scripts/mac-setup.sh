#!/usr/bin/env bash
# One-time Mac setup to route all Claude Code API traffic through the
# cc-nerf-buster transparent proxy running on the homelab runner VM.
#
# Prerequisite: ~/cc-nerf-buster-ca.crt must already be present.
# Fetch it via:
#   ssh ops 'ssh deploy@192.168.7.208 "docker exec $(docker ps -qf ancestor=192.168.7.208:5000/cc-nerf-buster:latest) cat /data/ca.crt"' > ~/cc-nerf-buster-ca.crt
#
# Safe to re-run — all mutations are idempotent.

set -euo pipefail

PROXY_HOST="192.168.7.208"
LOCAL_CA="${HOME}/cc-nerf-buster-ca.crt"
HOSTS_ENTRY="${PROXY_HOST}  api.anthropic.com"
NODE_CA_LINE="export NODE_EXTRA_CA_CERTS=${LOCAL_CA}"

if [[ ! -f "${LOCAL_CA}" ]]; then
  echo "ERROR: ${LOCAL_CA} not found. Fetch it first (see script header)." >&2
  exit 1
fi

echo "==> Trusting CA in macOS system keychain..."
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain "${LOCAL_CA}"
echo "    Trusted."

for rc in "${HOME}/.zshrc" "${HOME}/.bashrc" "${HOME}/.bash_profile"; do
  echo "==> Adding NODE_EXTRA_CA_CERTS to ${rc}..."
  if grep -qF "${NODE_CA_LINE}" "${rc}" 2>/dev/null; then
    echo "    Already present — skipping."
  else
    echo "${NODE_CA_LINE}" >> "${rc}"
    echo "    Added."
  fi
done

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
echo "  1. Reload your shell:  source ~/.zshrc  (or open a new terminal)"
echo "  2. Verify the proxy cert is serving:"
echo "     curl -sv https://api.anthropic.com 2>&1 | grep -E 'SSL|issuer|subject|Connected'"
echo "     You should see 'issuer: O=cc-nerf-buster' in the output."
