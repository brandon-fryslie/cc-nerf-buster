# cc-nerf-buster

`cc-nerf-buster` is a local MITM proxy for Anthropic traffic. It records request usage, exposes Prometheus metrics, and estimates 5-hour and 7-day quota capacity.

## Build And Verify

```bash
just build
just test
just vet
```

## Capacity-Probe Workflow

The capacity probe sends normal Claude requests through the proxy and measures:

- token deltas from proxy counters
- quota utilization bucket changes
- low / midpoint / high quota bounds when a tick is bracketed

Run a probe:

```bash
just probe-5h
just probe-7d
just probe
```

Resume the most recent matching run:

```bash
just probe-5h --continue
just probe-7d --continue
just probe --continue
```

Recompute report artifacts for an existing run:

```bash
just probe-report /absolute/path/to/run
```

Print the machine-readable low / midpoint / high bounds for an existing run:

```bash
just probe-bounds /absolute/path/to/run
```

## Bounds Workflow

Each quota tick is only observed after the boundary has already been crossed. That means every measured tick is bracketed by:

- `pre`: the last observation before the tick
- `post`: the first observation after the tick

This produces three estimates:

- `low` / `pre-pre`: biased low because it stops before the true boundary
- `midpoint`: recommended estimate using the midpoint between the `pre` and `post` costs
- `high` / `post-post`: biased high because it includes overshoot past the true boundary

The canonical derived bounds artifact is:

- `bounds.json`

It is written into each probe run directory by `tools/capacity-probe/report.py`.

The human-readable report is:

- `report.md`

The canonical raw inputs remain:

- `snapshots.jsonl`
- `raw-metrics/`

Everything else is derived from those files.

## Probe Run Artifacts

Each run directory contains:

- `manifest.json`: run configuration and baseline snapshot
- `iterations.jsonl`: one row per probe iteration
- `snapshots.jsonl`: parsed `/metrics` snapshots
- `raw-metrics/`: raw Prometheus bodies
- `prompts/`: exact prompt text sent
- `claude-output/`: raw Claude CLI output
- `crossings.jsonl`: detected quota boundary crossings
- `measured_ticks.jsonl`: clean measured ticks with `pre`, `midpoint`, and `post`
- `bounds.json`: machine-readable low / midpoint / high bounds
- `report.md`: human-readable summary

## Interpreting Token Counts

`bounds.json` and `report.md` express capacity in weighted USD and token projections.

For Opus, the most useful fields are usually:

- full quota in input-equivalent tokens
- per-1%-tick input-equivalent tokens

Use:

- `low` for a conservative lower bound
- `midpoint` as the best estimate
- `high` for the systematic upper bound
