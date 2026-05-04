# Capacity Probe Report

- Run directory: `/Users/bmf/.local/cc-nerf-buster/probe-runs/20260427T214538Z`
- Started:       `20260427T214538Z` UTC
- Model pinned:  `claude-opus-4-7`
- Metrics URL:   `http://localhost:57644/metrics`
- Iterations:    54
- Window:        `5h`
- Target 5h ticks: 3 (needs 4 crossings)
- Target 7d ticks: 1 (needs 2 crossings)
- Observed 5h crossings: 4 → 3 clean measured tick(s)
- Observed 7d crossings: 1 → 0 clean measured tick(s)

## Baseline → Final

| Field   | Baseline        | Final           | Δ              |
|---------|-----------------|-----------------|----------------|
| util_5h | 0.14        | 0.18        | +0.0400       |
| util_7d | 0.02        | 0.03        | +0.0100       |
| cost    | 0.007277 | 11.585551 | +11.578275 |

## Capacity Estimates

Internal unit: weighted-price-dollar-equivalent. Three estimates are compared:

- **Midpoint** — recommended. For each tick boundary K, estimate the true cost at K as `(c_pre + c_post) / 2`. Per-tick capacity = `(mid_K+1 − mid_K) / 0.01`. Removes the systematic bias that pre/post alone have.
- **Post-post** — uses the first snapshot after each boundary crossing. Overshoots (bias: high) but partially cancels if call sizes are uniform.
- **Pre-pre** — uses the last snapshot before each boundary crossing. Undershoots (bias: low).

Proxy lifetime estimate (exposed at `/metrics`, accumulated across all observations since the proxy started — includes data from before this probe run):

- 5h capacity (proxy): `206.494602` (weighted-USD)
- 7d capacity (proxy): `413.780150` (weighted-USD)

### Probe-derived capacity (this run only)

| Window | Method     | Capacity (weighted-USD) | # ticks |
|--------|------------|-------------------------|---------|
| 5h | midpoint   |              289.763333 |       3 |
| 5h | post-post  |              289.763333 |       3 |
| 5h | pre-pre    |              289.763333 |       3 |
| 7d | midpoint   |                       — |       0 |
| 7d | post-post  |                       — |       0 |
| 7d | pre-pre    |                       — |       0 |

### Bounds

Low = pre-pre, midpoint = recommended, high = post-post.

#### 5h

| Bound | Weighted-USD | Input tokens (full quota) | Input tokens / 1% tick |
|-------|--------------|---------------------------|------------------------|
| Low   | 289.763333 | 57,952,667 | 579,527 |
| Mid   | 289.763333 | 57,952,667 | 579,527 |
| High  | 289.763333 | 57,952,667 | 579,527 |

#### 7d

_(no clean measured ticks in this run — only the proxy lifetime estimate is available)_

### Capacity as tokens (midpoint estimate, this run)

#### 5h

| Model   | Input tokens        | Output tokens       |
|---------|---------------------|---------------------|
| haiku   |         289,763,333 |          57,952,667 |
| sonnet  |          96,587,778 |          19,317,556 |
| opus    |          57,952,667 |          11,590,533 |

#### 7d

_(no clean measured ticks in this run — no probe-derived number available. The proxy lifetime estimate above is still valid.)_

## Measured Ticks (per-tick breakdown)

One row per clean measured tick. A clean tick is bracketed by two consecutive boundary crossings with no multi-tick jumps. Compare the three capacity columns to see the spread — they should cluster tightly if the probe is well-calibrated.

### 5h

| # | tick        | cap_midpoint | cap_post | cap_pre |
|---|-------------|--------------|----------|---------|
| 1 | 0.15 → 0.16 | 286.6073     | 286.6073 | 286.6073 |
| 2 | 0.16 → 0.17 | 296.0755     | 296.0755 | 296.0755 |
| 3 | 0.17 → 0.18 | 286.6072     | 286.6072 | 286.6072 |

### 7d

_(no clean measured ticks in this run)_

## Provenance / audit trail

- `manifest.json`        — run config, baseline snapshot
- `snapshots.jsonl`      — every `/metrics` scrape (parsed JSON)
- `raw-metrics/`         — verbatim Prometheus exposition bodies
- `iterations.jsonl`     — every `claude -p` invocation (prompt, exit, wall time)
- `prompts/`             — exact prompt text sent per iteration
- `claude-output/`       — literal stdout+stderr per iteration
- `crossings.jsonl`      — every detected tick-boundary crossing (derived)
- `measured_ticks.jsonl` — clean measured ticks with all three capacity estimates (derived)
- `bounds.json`          — machine-readable low/mid/high bounds (derived)
- `probe.sh`             — thin shell wrapper as-run
- `probe.py`             — the Python probe driver as-run
- `report.py`            — this script as-run
- `scripts.sha256`       — SHA-256 of driver and report script

The canonical source is `snapshots.jsonl` + `raw-metrics/`. Everything else is reproducible by re-running `report.py <run_dir>`.
