# Security

`cc-nerf-buster` is a TLS-intercepting ("MITM") proxy. Understanding what that means is a prerequisite to running it safely.

## What the tool does to your machine

1. **Generates a local root CA** (`ca.crt` + `ca.key`) under `~/.local/cc-nerf-buster/` — or `$XDG_DATA_HOME/cc-nerf-buster/` — on first run. The CA key never leaves your machine.
2. **Mints per-host leaf certificates on the fly** signed by that CA whenever Claude Code opens a `CONNECT` tunnel to a configured upstream (default: `api.anthropic.com`).
3. **Asks you to trust the CA** so Claude Code accepts those leaf certs as valid TLS. That trust is what lets the proxy see decrypted request/response bodies.
4. **Logs every intercepted request to `usage.jsonl`** under the data directory. That file contains prompt content, response content, model names, and token counts.

None of this is a side effect — it's the whole point. The tool cannot extract usage or estimate quota without decrypting the traffic.

## Threat model

- **Your machine, your traffic only.** Run it on hardware you own and control. The CA is a system-level trust anchor; anything it signs will be accepted as authentic by every tool using the system trust store.
- **Do not trust the CA on devices you don't control.** Never copy `ca.crt` to a shared machine, a work laptop you don't administer, a CI runner, or a VM you hand to someone else. Anyone with the CA private key could impersonate any TLS host for you.
- **Do not publish or commit `ca.key`.** It is generated with `0600` permissions under the data directory for a reason. The `.gitignore` in this repo excludes the default data dir paths, but double-check before committing if you've relocated it.
- **`usage.jsonl` contains prompt and response content.** Treat it as sensitive. Don't paste it into bug reports without redacting.
- **The proxy binds to `localhost`.** It does not listen on external interfaces. Don't change that unless you know exactly what you're doing and you've firewalled the port.

## Trusting the CA

### macOS

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  ~/.local/cc-nerf-buster/ca.crt
```

### Linux (Debian / Ubuntu)

```bash
sudo cp ~/.local/cc-nerf-buster/ca.crt /usr/local/share/ca-certificates/cc-nerf-buster.crt
sudo update-ca-certificates
```

### Linux (Fedora / RHEL)

```bash
sudo cp ~/.local/cc-nerf-buster/ca.crt /etc/pki/ca-trust/source/anchors/cc-nerf-buster.crt
sudo update-ca-trust
```

### Linux (Arch)

```bash
sudo trust anchor --store ~/.local/cc-nerf-buster/ca.crt
```

Some tools (Node, Python `requests`, `curl`, `git`) read their own CA bundle rather than the system store. The install script and startup banner print the env vars (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`) that point those at `ca.crt`.

## Revoking the CA

When you're done with the tool, remove the trust.

### macOS

```bash
sudo security delete-certificate -c "cc-nerf-buster" /Library/Keychains/System.keychain
```

(The CA's common name is `cc-nerf-buster`. Confirm with `openssl x509 -in ~/.local/cc-nerf-buster/ca.crt -noout -subject`.)

### Linux (Debian / Ubuntu)

```bash
sudo rm /usr/local/share/ca-certificates/cc-nerf-buster.crt
sudo update-ca-certificates --fresh
```

### Linux (Fedora / RHEL)

```bash
sudo rm /etc/pki/ca-trust/source/anchors/cc-nerf-buster.crt
sudo update-ca-trust
```

### Linux (Arch)

```bash
sudo trust anchor --remove ~/.local/cc-nerf-buster/ca.crt
```

Then delete the data directory to destroy the CA key material:

```bash
rm -rf ~/.local/cc-nerf-buster
```

`just uninstall` performs the uninstall steps for the binary and data directory; system trust-store revocation is manual because it's outside the tool's scope.

## Reporting a vulnerability

If you find a bug that undermines the threat model above — for example, the proxy accepting external connections, the CA key leaking outside the data directory, or `usage.jsonl` being written with overly permissive mode — open a GitHub issue. There is no private disclosure channel; this is a single-machine developer tool with no production surface.
