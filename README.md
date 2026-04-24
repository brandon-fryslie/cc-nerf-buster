# cc-nerf-buster

`cc-nerf-buster` is a local MITM proxy for Anthropic traffic. It records request usage, exposes Prometheus metrics, and estimates 5-hour and 7-day quota capacity.

## Build And Verify

```bash
just build
just test
just vet
```

## Capacity-Probe Workflow

Normal usage is one command:

```bash
just probe
```

Warning: do not use the same Claude account for anything else while the probe is running. If other tracked or untracked usage happens at the same time, the quota utilization numbers will include both and the resulting bounds will be wrong.

When it exits, it already prints:

- the active org/upstream scope
- 5h low / midpoint / high bounds
- 7d low / midpoint / high bounds
- pinned-model full-quota and per-1%-tick token projections

If you only want one window:

```bash
just probe-5h
just probe-7d
```

If you interrupt the probe, it still prints the same summary and then prints the exact resume command. You can also resume explicitly with:

```bash
just probe --continue
```

## Bounds Workflow

Each quota tick is only visible after it has already been crossed, so every measured tick is bracketed by:

- `pre`: the last observation before the tick
- `post`: the first observation after the tick

That produces three estimates:

- `low` / `pre-pre`: lower bound
- `midpoint`: recommended estimate using the midpoint between the `pre` and `post` costs
- `high` / `post-post`: upper bound

## Probe Run Artifacts

Each run directory includes:

- `manifest.json`: run configuration and baseline snapshot
- `bounds.json`: machine-readable low / midpoint / high bounds
- `report.md`: human-readable summary
- `snapshots.jsonl` and `raw-metrics/`: canonical raw inputs

## Advanced / Rebuild Artifacts

These are optional maintenance commands, not part of the normal workflow.

Resume the most recent matching run:

```bash
just probe --continue
```

Rebuild artifacts for an existing run:

```bash
just probe-report /absolute/path/to/run
just probe-bounds /absolute/path/to/run
```

## Interpreting Token Counts

`bounds.json` and `report.md` express capacity in weighted USD and token projections. For Opus, the most useful numbers are usually full quota in input-equivalent tokens and per-1%-tick input-equivalent tokens.
