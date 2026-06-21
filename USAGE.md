# Usage

How to install `cc-nerf-buster` and reproduce the quota measurements documented in [`README.md`](README.md).

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

`just install` builds the binary into `~/.local/bin/cc-nerf-buster` and configures a local data dir under `~/.local/cc-nerf-buster/` (or `$XDG_DATA_HOME/cc-nerf-buster`).

Uninstall with `just uninstall`.

## Run The Fresh Quota Probe

The fresh quota probe is the authoritative measurement path. It launches Claude Code with the proxy already wired up via environment variables scoped to the spawned process, drives it against a single account, and estimates quota capacity from the run's canonical `usage.jsonl` event stream.

Run one quota window at a time:

```bash
just quota-5h
just quota-7d
```

> [!WARNING]
> **Do not use the same Claude account for anything else while the probe is running.** If other tracked or untracked usage happens concurrently, the quota numbers will include both and the resulting bounds will be wrong.

When it exits, it prints:

- The active org/upstream scope
- The selected window's low / midpoint / high bounds
- Weighted USD per 1% tick and full-window capacity
- Opus cache-write token projections per 1% tick and full quota

Dry-runs exercise the artifact and estimator path without spending quota:

```bash
just quota-dry-5h
just quota-dry-7d
```

Regenerate the fresh report from an existing run directory:

```bash
just quota-report /absolute/path/to/run 5h
just quota-report /absolute/path/to/run 7d
```

## Bounds

Each quota tick is only visible after it's been crossed, so every crossing is bracketed by:

- `pre`: the last observation before the tick
- `post`: the first observation after the tick

The estimator pairs crossings to eliminate the unknown quota usage that existed before the probe started. The final interval is:

- `low`: lower bound of all pairwise crossing constraints
- `midpoint`: selected estimate
- `high`: upper bound of all pairwise crossing constraints

## Probe Run Artifacts

Each run directory contains:

- `manifest.json` — run configuration and baseline snapshot
- `usage.jsonl` — canonical proxy event stream
- `fresh-bounds.json` — machine-readable low / midpoint / high bounds
- `fresh-report.md` — human-readable summary
- `prompts/` and `outputs/` — exact driver inputs and Claude outputs
- `traffic.har` and `debug.jsonl` — proxy diagnostics

## Advanced / Rebuild Artifacts

Optional maintenance commands, not part of normal use.

```bash
just quota-report /absolute/path/to/run 5h  # recompute fresh bounds/report
just quota-test                             # Python estimator/driver tests
just build && just test && just vet         # build and verify the proxy itself
```

## Interpreting Token Counts

`fresh-bounds.json` and `fresh-report.md` express capacity as weighted USD and token projections. For Opus the useful numbers are usually cache-write-equivalent tokens per 1% tick and full-window cache-write-equivalent tokens.

## Legacy Capacity Probe

The older `tools/capacity_probe/` workflow remains available for comparison:

```bash
just probe-5h
just probe-7d
just probe-dry-5h
just probe-dry-7d
```

New measurements should use `just quota-5h` or `just quota-7d`.
