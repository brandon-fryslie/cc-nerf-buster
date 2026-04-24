# Usage

How to install `cc-nerf-buster`, run the proxy, and reproduce the quota measurements documented in [`README.md`](README.md).

Security model (local CA generation, trust-store implications, revocation) is covered separately in [`SECURITY.md`](SECURITY.md). Read it first.

## Requirements

- Go 1.25+
- Python 3 (for the capacity probe)
- [`just`](https://github.com/casey/just)
- Claude Code

## Install

```bash
git clone https://github.com/brandon-fryslie/cc-nerf-buster
cd cc-nerf-buster
just install
```

`just install` builds the binary into `~/.local/bin/cc-nerf-buster`, generates a CA under `~/.local/cc-nerf-buster/` (or `$XDG_DATA_HOME/cc-nerf-buster`), and prints the env block to paste into your shell.

Trust the generated CA system-wide (required once). macOS:

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  ~/.local/cc-nerf-buster/ca.crt
```

For Linux and for revoking trust later, see [`SECURITY.md`](SECURITY.md).

Uninstall with `just uninstall`.

## Run The Proxy

```bash
cc-nerf-buster
```

It listens on `:9480` (proxy) and `:9481/metrics` (Prometheus). Point Claude Code at it — the startup banner prints the exact env vars and `~/.claude/settings.json` snippet to use.

A one-shot convenience target that launches Claude Code with all the right env already set:

```bash
just login
```

## Build And Verify

```bash
just build
just test
just vet
```

## Capacity Probe

The probe is what generates the numbers in the README. It drives Claude Code in a loop against a single account, watches the utilization gauge tick, and brackets each tick with pre/post observations.

Normal usage is one command:

```bash
just probe
```

> Do not use the same Claude account for anything else while the probe is running. If other tracked or untracked usage happens concurrently, the quota numbers will include both and the resulting bounds will be wrong.

When it exits, it prints:

- The active org/upstream scope
- 5h low / midpoint / high bounds
- 7d low / midpoint / high bounds
- Pinned-model full-quota and per-1%-tick token projections

One window at a time:

```bash
just probe-5h
just probe-7d
```

If you interrupt the probe, it still prints the summary and the exact resume command. Explicit resume:

```bash
just probe --continue
```

## Bounds

Each quota tick is only visible after it's been crossed, so every measured tick is bracketed by:

- `pre`: the last observation before the tick
- `post`: the first observation after the tick

That produces three estimates:

- `low` / `pre-pre`: lower bound
- `midpoint`: recommended estimate (midpoint of `pre`/`post` cost)
- `high` / `post-post`: upper bound

## Probe Run Artifacts

Each run directory contains:

- `manifest.json` — run configuration and baseline snapshot
- `bounds.json` — machine-readable low / midpoint / high bounds
- `report.md` — human-readable summary
- `snapshots.jsonl` and `raw-metrics/` — canonical raw inputs

## Advanced / Rebuild Artifacts

Optional maintenance commands, not part of normal use.

```bash
just probe --continue                       # resume most recent matching run
just probe-report /absolute/path/to/run     # rebuild report.md
just probe-bounds /absolute/path/to/run     # recompute bounds
```

## Interpreting Token Counts

`bounds.json` and `report.md` express capacity as weighted USD and token projections. For Opus the useful numbers are usually full-quota input-equivalent tokens and per-1%-tick input-equivalent tokens.
